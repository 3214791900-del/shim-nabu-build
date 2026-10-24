#!/usr/bin/env python3
"""Apply the NABU bring-up changes to an iOS root made by tools/mkiosroot.sh.

  sudo nabu-root-tweaks.py MOUNTED_ROOT

MOUNTED_ROOT is the HFS+ image mounted read-write (Linux: run
`fsck.hfsplus -fy` on the image, then `mount -t hfsplus -o loop,force`).
Run as root (it writes root/mobile owned files). Idempotent. This is what
turned the plain mkiosroot.sh output into the images that reach the home
screen (image versions v8-v14 in NOTES.md):

  launchd.plist   _PanicOnCrash -> _PanicOnCrasX (a crashing daemon must not
                  panic the system); CommCenter jobs removed (no baseband: it
                  crash-loops and Setup blocks on its CoreTelephony XPC)
  containers      /private/var/containers/{Bundle,Data/System,Shared/SystemGroup,Temp}
                  (on iOS they live on the data volume)
  migration       LastSystemVersion so DataMigration does not run for an hour
                  on every boot
  audio           VirtualAudio HAL plug-in disabled (mediaserverd crash)
  MediaToolbox    StartRecorderServer=false (camera filter prewarm calls a
                  NULL OpenGL function without a GPU -> mediaserverd crash loop)
  SpringBoard     BKNoWatchdogs (one slow CPU path, no scene-create watchdog
                  kills), no cloud-configuration UI
  Setup           skipped: SetupDone, SetupFinishedAllSteps, SetupVersion 11,
                  mini-buddy flags off
  activation      mobileactivationd data ark seeded; the actual Activated state
                  comes from its Hactivation branch, selected by the /chosen
                  security-status properties in tools/mkadt.py
"""
import glob, os, plistlib, shutil, sys

BUILD, VERSION = '20H392', '16.7.16'
MOBILE = 501


def system_version(root):
    """Use the build/version actually present in the image.

    The port boots more than one Apple OS release (iPhone X iOS 16.7.16 and
    iPad7,3 iPadOS 16.6.1), so DataMigration and the activation ark must match
    the root, not a hard-coded build.
    """
    path = os.path.join(root, 'System/Library/CoreServices/SystemVersion.plist')
    try:
        data = load(path)
        return (data.get('ProductBuildVersion') or BUILD,
                data.get('ProductVersion') or VERSION)
    except Exception:
        return BUILD, VERSION


def load(path):
    with open(path, 'rb') as f:
        return plistlib.load(f)


def chown_chmod(path, uid, gid, mode):
    """Ownership is required on the device; tolerate a non-root dry run."""
    try:
        os.chown(path, uid, gid)
    except (AttributeError, NotImplementedError):
        pass
    except PermissionError:
        if os.name == 'posix' and os.geteuid() == 0:
            raise
    os.chmod(path, mode)


def save(path, data, uid, gid, mode, fmt=plistlib.FMT_BINARY):
    with open(path, 'wb') as f:
        plistlib.dump(data, f, fmt=fmt)
    chown_chmod(path, uid, gid, mode)


def merge_pref(root, name, values):
    """Merge keys into /var/mobile/Library/Preferences/NAME.plist (mobile, 0600)."""
    d = os.path.join(root, 'private/var/mobile/Library/Preferences')
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name + '.plist')
    data = load(path) if os.path.exists(path) else {}
    data.update(values)
    save(path, data, MOBILE, MOBILE, 0o600)
    print('pref', name, sorted(values))


def launchd(root):
    path = os.path.join(root, 'System/Library/xpc/launchd.plist')
    st = os.stat(path)
    plist = load(path)
    jobs = plist['LaunchDaemons']
    renamed = 0
    for job in jobs.values():
        if '_PanicOnCrash' in job:
            job['_PanicOnCrasX'] = job.pop('_PanicOnCrash')
            renamed += 1
    removed = [k for k, j in jobs.items() if j.get('Label') == 'com.apple.CommCenter']
    for k in removed:
        del jobs[k]
    save(path, plist, st.st_uid, st.st_gid, st.st_mode & 0o7777)
    print(f'launchd.plist: {renamed} _PanicOnCrash renamed, {len(removed)} CommCenter jobs removed')


def containers(root):
    base = os.path.join(root, 'private/var/containers')
    for sub, uid in (('Bundle', 33), ('Data/System', 0), ('Shared/SystemGroup', 0), ('Temp', 0)):
        d = os.path.join(base, sub)
        if not os.path.isdir(d):
            os.makedirs(d)
            chown_chmod(d, uid, uid, 0o755)
            print('mkdir', d)


def find_container(root, kind, identifier):
    for meta in glob.glob(os.path.join(root, 'private/var/containers', kind, '*',
                                       '.com.apple.mobile_container_manager.metadata.plist')):
        if load(meta).get('MCMMetadataIdentifier') == identifier:
            return os.path.dirname(meta)
    return None


def activation(root, build=BUILD, version=VERSION):
    c = find_container(root, 'Data/System', 'com.apple.mobileactivationd')
    if c is None:
        print('warning: no mobileactivationd system container in the image; skipped data ark')
        return
    d = os.path.join(c, 'Library/internal')
    os.makedirs(d, exist_ok=True)
    chown_chmod(d, MOBILE, MOBILE, 0o755)
    save(os.path.join(d, 'data_ark.plist'), {
        '-ActivationState': 'Activated', '-BrickState': False,
        '-BuildVersion': build, '-ProductVersion': version,
        '-DataMigrationCompleted': True, '-PreSydroDataMigrationCompleted': True,
    }, MOBILE, MOBILE, 0o600)
    print('data ark', d)


def audio(root):
    src = os.path.join(root, 'Library/Audio/Plug-Ins/HAL/VirtualAudio.plugin')
    dst_dir = os.path.join(root, 'Library/Audio/Plug-Ins/HAL.disabled')
    if os.path.exists(src):
        os.makedirs(dst_dir, exist_ok=True)
        shutil.move(src, dst_dir)
        print('VirtualAudio.plugin disabled')
    for conf in glob.glob(os.path.join(root, 'System/Library/Frameworks/MediaToolbox.framework/*/SubServerConfiguration.plist')):
        st = os.stat(conf)
        data = load(conf)
        data['StartRecorderServer'] = False
        save(conf, data, st.st_uid, st.st_gid, st.st_mode & 0o7777, plistlib.FMT_XML)
        print('StartRecorderServer off', os.path.relpath(conf, root))


def display_env(root):
    """Give CoreAnimation a CPU-only renderer path.

    The porter has no GPU driver, so CA never builds a renderer and backboardd
    submits black frames.  QuartzCore reads a pile of CA_* environment
    variables; forcing the local server and the timer keeps the render loop
    inside backboardd instead of waiting for a render-server/GPU handshake.
    Overridable through NABU_CA_ENV (comma separated K=V) for experiments.
    """
    import os as _os
    env = {'CA_FORCE_LOCAL_SERVER': '1', 'CA_FORCE_TIMER': '1'}
    extra = _os.environ.get('NABU_CA_ENV', '')
    for item in extra.split(','):
        if '=' in item:
            k, v = item.split('=', 1)
            env[k.strip()] = v.strip()
    path = os.path.join(root, 'System/Library/xpc/launchd.plist')
    st = os.stat(path)
    plist = load(path)
    jobs = plist['LaunchDaemons']
    hit = 0
    for job in jobs.values():
        if job.get('Label') in ('com.apple.backboardd', 'com.apple.SpringBoard'):
            job.setdefault('EnvironmentVariables', {}).update(env)
            hit += 1
    save(path, plist, st.st_uid, st.st_gid, st.st_mode & 0o7777)
    print('display env on %d jobs: %s' % (hit, ', '.join(sorted(env))))


def main():
    if len(sys.argv) != 2 or not os.path.isdir(os.path.join(sys.argv[1], 'System/Library/xpc')):
        sys.exit(__doc__)
    root = sys.argv[1]
    build, version = system_version(root)
    print(f'root image: iOS/iPadOS {version} ({build})')
    launchd(root)
    containers(root)
    activation(root, build, version)
    display_env(root)
    audio(root)
    merge_pref(root, 'com.apple.migration', {
        'LastSystemVersion': build,
        'DMUserDataDisposition': {'SyntheticDidUpgrade': False, 'buildVersion': build}})
    merge_pref(root, 'com.apple.springboard', {
        'BKNoWatchdogs': True,
        'FBLaunchWatchdogExceptions': {'com.apple.purplebuddy': 100.0},
        'MCForceShowCloudConfigurationUI': False})
    merge_pref(root, 'com.apple.purplebuddy', {
        'SetupDone': True, 'SetupFinishedAllSteps': True, 'SetupVersion': 11,
        'daemonizeNeedsToRun': False})
    merge_pref(root, 'com.apple.purplebuddy.notbackedup', {
        k: False for k in ('ShouldRunDiagnosticsMiniBuddy', 'ShouldRunHSA2MiniBuddy',
                           'ShouldRunKeychainSyncMiniBuddy', 'ShouldRunMesaMiniBuddy',
                           'ShouldRunPaymentMiniBuddy', 'ShouldRunStandardMiniBuddy',
                           'SilentICDPUpgrade')})
    merge_pref(root, 'com.apple.managedconfiguration.notbackedup', {
        'LockdownActivationIndicatesCloudConfigurationAvailable': False})
    verify(root, build)


def verify(root, build):
    """Fail the build when the files that gate SpringBoard did not land.

    The two ways this has been got wrong before: the build version written was
    not the one in the image (DataMigration then re-runs on every boot and
    backboardd never releases the system-shell check-in), and the migration
    state went into com.apple.DataMigration.plist / the ByHost variant, which
    the daemon does not read.
    """
    pref = os.path.join(root, 'private/var/mobile/Library/Preferences')
    checks = {
        'com.apple.migration.plist': lambda d: d.get('LastSystemVersion') == build,
        'com.apple.purplebuddy.plist': lambda d: d.get('SetupDone') is True,
        'com.apple.springboard.plist': lambda d: d.get('BKNoWatchdogs') is True,
    }
    bad = []
    for name, ok in checks.items():
        try:
            good = ok(load(os.path.join(pref, name)))
        except Exception as exc:
            good = False
            print(f'  {name}: {exc}')
        print(f'verify {name}: {"OK" if good else "FAIL"}')
        if not good:
            bad.append(name)
    for d in ('Bundle', 'Data/System'):
        p = os.path.join(root, 'private/var/containers', d)
        if os.path.isdir(p):
            print(f'verify containers/{d}: OK')
        else:
            print(f'verify containers/{d}: FAIL')
            bad.append(f'containers/{d}')
    if bad:
        sys.exit('nabu-root-tweaks: verification failed: ' + ', '.join(bad))
    print(f'nabu-root-tweaks: OK for {build}')


if __name__ == '__main__':
    main()
