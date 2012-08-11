import argparse
from subprocess import check_call, check_output, Popen, STDOUT, PIPE
import os, re, string, sys
import plistlib
from tempfile import mkdtemp
from fnmatch import fnmatch
import shutil
import pipes

try:
    import clint.textui
    puts = clint.textui.puts
except ImportError:
    import util
    puts = util.puts

#### stolen from provtool https://github.com/mindsnacks/provtool

DEFAULT_PROVPROF_DIR = os.path.expanduser('~/Library/MobileDevice/Provisioning Profiles')

def is_prov_profile(filePath):
    return filePath.endswith('.mobileprovision')

def plist_string_from_prov_profile_path(path):
    beginToken = '<?xml'
    endToken = '</plist>'
    f = open(path)
    data = f.read()
    begin = data.index(beginToken)
    end = data.rindex(endToken) + len(endToken) 
    return data[begin:end]

def name_from_prov_profile_path(filePath):
    plistString = plist_string_from_prov_profile_path(filePath)
    plist = plistlib.readPlistFromString(plistString)
    return plist['Name']

def find_prov_profile_by_name(name, dir=DEFAULT_PROVPROF_DIR):
    if not os.path.exists(dir): return None
    for f in os.listdir(dir):
        if is_prov_profile(f):
            path = os.path.join(dir, f)
            if name == name_from_prov_profile_path(path):
                return path
    return None

#### end provtool

def _shellify(args):
    return " ".join(pipes.quote(s) for s in args)

def _join_cmds(*cmds):
    return " ; ".join(cmds)

def _parse_setenv_var(var, text):
    match = re.search(r'(setenv %s )(.*)' % var, text).group(2)
    # strip "'s if they exist
    return string.strip(match, '"')

def _find_prov_profile(input):
    """Tries to find a provisioning profile using a few methods, and returns
    it's path if found"""

    # check if it's a valid path first
    if os.path.exists(input):
        return os.path.abspath(input)

    # assume it's a name of a provisioning profile
    path = find_prov_profile_by_name(input)

    return path

def _list_keychains():
    security_output = check_output(['security', 'list-keychains'])
    keychains = set([k.strip()[1:-1] for k in security_output.split('\n') if len(k) > 0])
    return keychains

def _add_keychain(keychain_path):
    keychain_path = os.path.abspath(keychain_path)
    keychains = _list_keychains()
    keychains.add(keychain_path)
    cmd = ['security', 'list-keychains', '-s']
    cmd.extend(list(keychains))
    check_call(cmd)

def _unlock_keychain_cmd(keychain_path, password):
    args = ['security', '-v', 'unlock-keychain', '-p', password,
        os.path.abspath(keychain_path)]
    return _shellify(args)

def _unlock_keychain(keychain_path, password):
    cmd = _unlock_keychain_cmd(keychain_path, password)
    check_call(cmd, shell=True)

def debug(args):
    pass

def ipa(args):
    """http://stackoverflow.com/questions/6896029/re-sign-ipa-iphone"""

    prov_profile_path = _find_prov_profile(args.profile)
    if prov_profile_path is None:
        # TODO: better error handling
        print "couldn't find profile"
        sys.exit(1)
   
    build_args = ['xcodebuild', '-sdk', 'iphoneos']
    if args.project is not None:
        build_args.extend(['-project', args.project])
    build_args.extend([
        '-target', args.target, 
        '-config', args.config, 
        #'build', 
        'CODE_SIGN_IDENTITY=%s' % (args.identity)])
    if args.keychain is not None:
        _add_keychain(args.keychain)
        build_args.extend(['OTHER_CODE_SIGN_FLAGS=--keychain=%s' %
            os.path.abspath(args.keychain)])

    should_unlock_keychain = args.keychain is not None and args.keychain_password is not None
       
    build_cmd = _shellify(build_args)
    if should_unlock_keychain:
        # unlocking keychain in the same shell to try to prevent 
        # "User Interaction is Not Allowed" errors
        unlock_keychain_cmd = _unlock_keychain_cmd(
                args.keychain, args.keychain_password)
        build_cmd = _join_cmds(unlock_keychain_cmd, build_cmd)
    
    p = Popen(build_cmd, stderr=STDOUT, stdout=PIPE, shell=True)
    build_output = ''
    while True:
        line = p.stdout.readline()
        if not line: break
        build_output += line
        puts(line, newline=False)
    p.wait()
    if p.returncode != 0:
        print "Process exited with non-zero status " + str(p.returncode)
        sys.exit(1)

    built_products_dir = _parse_setenv_var('BUILT_PRODUCTS_DIR', build_output)
    full_product_name = _parse_setenv_var('FULL_PRODUCT_NAME', build_output)
    full_product_path = os.path.join(built_products_dir, full_product_name)

    # unlock the keychain again
    if args.keychain_password is not None:
        _unlock_keychain(args.keychain, args.keychain_password)

    package_args = ['xcrun', '-v', 
            '-sdk', 'iphoneos',
            'PackageApplication', full_product_path,
            '--sign', args.identity,
            '--embed', prov_profile_path]
    #if args.keychain is not None:
    #    package_args.extend(['--keychain=%s' % os.path.abspath(args.keychain)])

    package_cmd = _shellify(package_args)
    print package_cmd
    if should_unlock_keychain:
        package_cmd = _join_cmds(unlock_keychain_cmd, package_cmd)

    check_call(package_cmd, shell=True)

    full_ipa_path = full_product_path[:-3] + 'ipa'
    output_path = os.path.abspath(args.output)
    shutil.move(full_ipa_path, output_path)

def resign(args):
    ipa_path = args.ipa
    if not os.path.exists(ipa_path):
        # TODO: better error
        print "couldn't find ipa"
        sys.exit(1)

    tmp_dir = mkdtemp()
    check_call(['unzip', ipa_path, '-d', tmp_dir])

    payload_path = os.path.join(tmp_dir, 'Payload')
    for file in os.listdir(payload_path):
        if fnmatch(file, '*.app'):
            app_path = os.path.join(payload_path, file)

    shutil.rmtree(os.path.join(app_path, '_CodeSignature'))

    embedded_prov_profile_path = os.path.join(app_path, 'embedded.mobileprovision')
    os.remove(embedded_prov_profile_path)

    src_prov_profile_path = _find_prov_profile(args.profile)
    shutil.copyfile(src_prov_profile_path, embedded_prov_profile_path)

    codesign_args = ['codesign', '-f', 
                '-s', args.identity,
                '--resource-rules',
                os.path.join(app_path, 'ResourceRules.plist'),
                '--entitlements',
                os.path.join(app_path, 'Entitlements.plist')]

    if args.keychain is not None:
        keychain_path = os.path.abspath(args.keychain)
        codesign_args.extend(['--keychain', keychain_path])

    codesign_args.extend([app_path])

    codesign_output = check_output(codesign_args)
    puts(codesign_output)

    output_path = os.path.abspath(args.output)

    # Change working dir so 'Payload' is at the root of the archive.
    # Might be a way to do this with args to zip but I couldn't find it.
    pwd = os.getcwd()
    os.chdir(tmp_dir)
    check_call(['zip', '-qr', output_path, 'Payload'])
    os.chdir(pwd)

    shutil.rmtree(tmp_dir)
    
def main():
    parser = argparse.ArgumentParser(description='')

    subparsers = parser.add_subparsers(title='subcommands',
            description='valid subcommands',
            help='additional help')

    # ipa
    parser_ipa = subparsers.add_parser('ipa', help='ipa help')
    parser_ipa.add_argument('--project', action='store', required=False)
    parser_ipa.add_argument('--target', action='store', required=True)
    parser_ipa.add_argument('--config', action='store', default='Debug', required=False)
    parser_ipa.add_argument('--identity', action='store', required=True)
    parser_ipa.add_argument('--profile', action='store', required=True)
    parser_ipa.add_argument('--keychain', action='store', required=False)
    parser_ipa.add_argument('--keychain-password', action='store', required=False)
    parser_ipa.add_argument('--output', action='store', required=True)
    parser_ipa.set_defaults(func=ipa)

    # resign
    parser_resign = subparsers.add_parser('resign', help='resign help')
    parser_resign.add_argument('--ipa', action='store', required=True)
    parser_resign.add_argument('--identity', action='store', required=True)
    parser_resign.add_argument('--profile', action='store', required=True)
    parser_resign.add_argument('--keychain', action='store', required=False)
    parser_resign.add_argument('--output', action='store', required=True)
    parser_resign.set_defaults(func=resign)

    parser_debug = subparsers.add_parser('debug', help='debug help')
    parser_debug.set_defaults(func=debug)
    
    args = parser.parse_args()
    args.func(args)

