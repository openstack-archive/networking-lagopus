#!/usr/bin/env python

# Copyright (C) 2014-2015 Nippon Telegraph and Telephone Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#
# lagosh.py - interactive shell for Lagopus vswitch.
# from https://github.com/hibitomo/lago-dsl
#

import cmd
import getopt
import inspect
import json
import os
import pydoc
import re
import select
import socket
import sys


class DSLError(Exception):

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)


class dsl(object):

    def decode_file(self, args):
        if args == []:
            lines = sys.stdin.read().splitlines()
        else:
            lines = open(args[0], 'r').read().splitlines()
        return self.decode(lines)

    def decode(self, lines):
        result = ''
        topcmd = ''
        for line in lines:
            line = re.sub(r'#.*$', r'', line)
            line = re.sub(r'\n', r'', line)
            line = re.sub(r'^.*(enable|disable)\s*$', r'', line)
            words = line.split()
            if words == []:
                continue
            # top level keywords
            if words[0] != topcmd:
                if topcmd != '':
                    result += '}\n'
                topcmd = words[0]
                result += topcmd + ' {\n'
            words.pop(0)
            # identifier
            if (words[0])[0] != '-':
                result += '\t'
                result += re.sub(r'^:([^:])', r'\1', words.pop(0)) + ' {\n'
                tab = '\t'
            else:
                tab = ''
            # params
            fmt = ''
            while words != []:
                word = re.sub(r'^:([^:])', r'\1', words.pop(0))
                if word == 'create':
                    continue
                if word[0] == '-':
                    word = word.replace('-', '', 1)
                    if fmt != '':
                        result += tab + '\t' + fmt + ';\n'
                    fmt = word
                else:
                    fmt = fmt + ' ' + word
            if fmt != '':
                result += tab + '\t' + fmt + ';\n'
            if tab != '':
                result += tab + '}\n'
        if topcmd != '':
            result += '}'
        return result

    def encode_file(self, args):
        if args == []:
            lines = sys.stdin.read().splitlines()
        else:
            lines = open(args[0], 'r').read().splitlines()
        return self.encode(lines)

    def encode(self, lines):
        result = ''
        wordlist = []
        for line in lines:
            line = re.sub(r'#.*$', r'', line)
            line = re.sub(r'\n', r'', line)
            wordlist.append(line.split())

        prewords = []
        childwords = []
        bridge = []
        encodedpre = 0
        linecount = 0
        for words in wordlist:
            linecount += 1
            for word in words:
                # add to childwords if normal word
                if word != '{' and not re.search(r';$', word) and word != '}':
                    childwords.append(word)
                    continue
                # set (add) prewords(repeated words) if block begin.
                if word == '{':
                    prewords.append(childwords)
                    if len(prewords) == 2:
                        if prewords[0][0] == 'bridge':
                            bridge.append(prewords[1][0])
                        prewords.append(['create'])
                    for decwords in prewords[encodedpre:]:
                        result += self.encode_id(decwords)
                    encodedpre = len(decwords)
                    childwords = []
                    continue
                # unset (remove) prewords if block end.
                if word == '}':
                    result += '\n'
                    try:
                        if len(prewords) == 3:
                            prewords.pop()
                        prewords.pop()
                    except Exception:
                        raise DSLError('line ' + str(linecount) +
                                       ': Unbalanced parenthesis')
                    childwords = []
                    encodedpre = 0
                    continue
                # ;
                childwords.append(word)
                result += self.encode_op(childwords)
                childwords = []
        if prewords != []:
            raise DSLError('line ' + str(linecount) +
                           ': End of input, unbalanced parenthesis')
        for word in bridge:
            result += 'bridge ' + word + ' enable\n'
        return result

    def encode_id(self, words):
        result = ''
        for word in words:
            result += word + ' '
        return result

    def encode_op(self, words):
        op = words.pop(0)
        result = '-' + op
        for word in words:
            result += ' ' + word
        return re.sub(r';$', ' ', result)


class ds_client(object):

    port = 12345

    def remove_namespace(self, arg):
        if isinstance(arg, dict):
            for a in arg.iterkeys():
                if isinstance(arg[a], unicode):
                    arg[a] = re.sub(r'^:([^:])', r'\1', arg[a])
                elif isinstance(arg[a], dict):
                    self.remove_namespace(arg[a])
                elif isinstance(arg[a], list):
                    for l in arg[a]:
                        self.remove_namespace(l)

    def open(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect(('127.0.0.1', self.port))

    def close(self):
        del self.sock

    def write(self, arg):
        self.sock.sendall(arg)

    def is_readable(self):
        return select.select([self.sock], [], [], 0) == ([self.sock], [], [])

    def read(self):
        data = ''
        while True:
            res = self.sock.recv(8192)
            data += res
            if not self.is_readable():
                try:
                    jdata = json.loads(data)
                    self.remove_namespace(jdata)
                    break
                except ValueError:
                    continue
        return jdata

    def request(self, arg):
        self.write(arg)
        jdata = self.read()
        if jdata['ret'] != 'OK':
            if 'file' in jdata and 'line' in jdata:
                raise DSLError(jdata['file'] + ':' + str(jdata['line']) +
                               ': ' + jdata['ret'] + ': ' + jdata['data'])
            else:
                raise DSLError(jdata['ret'] + ': ' + jdata['data'])
        return jdata['data']

    def lock(self):
        self.request('lock\n')

    def unlock(self):
        self.request('unlock\n')

    def call(self, arg, response=True):
        try:
            self.open()
        except Exception:
            raise
        self.write(arg)
        if response:
            jdata = self.read()
            self.close()
        else:
            self.close()
            return
        if jdata['ret'] != 'OK':
            if 'file' in jdata and 'line' in jdata:
                raise DSLError(jdata['file'] + ':' + str(jdata['line']) +
                               ': ' + jdata['ret'] + ': ' + jdata['data'])
            else:
                raise DSLError(jdata['ret'] + ': ' + jdata['data'])
        try:
            return jdata['data']
        except Exception:
            return


class Cmd(cmd.Cmd):

    pager = False

    def __init__(self, *args, **kwargs):
        cmd.Cmd.__init__(self, *args, **kwargs)

    def onecmd(self, line):
        try:
            return cmd.Cmd.onecmd(self, line)
        except socket.error:
            print('Socket connection refused.  Lagopus is not running?')

    def completenames(self, text, *ignored):
        dotext = 'do_' + text
        return [a[3:] for a in self.get_names()
                if a.startswith(dotext) and a != 'do_shell']

    def precmd(self, line):
        args = line.split()
        line = ''
        cmd = ''
        for a in args:
            begidx = len(line)
            endidx = begidx + len(a) + 1
            if begidx == 0:
                matches = self.completenames(a, line, begidx, endidx)
                if len(matches) == 1:
                    cmd = matches[0]
                else:
                    cmd = a
                line = cmd
            else:
                if cmd == '':
                    compfunc = self.completedefault
                else:
                    try:
                        compfunc = getattr(self, 'complete_' + cmd)
                    except AttributeError:
                        compfunc = self.completedefault
                matches = compfunc(a, line, begidx, endidx)
                if len(matches) == 1:
                    line += ' ' + matches[0]
                else:
                    line += ' ' + a
        return line

    def emptyline(self):
        return

    def output(self, line):
        if self.pager:
            pydoc.pager(line)
        else:
            print(line)

    def cmdloop(self, showpager=False):
        self.pager = showpager
        cmd.Cmd.cmdloop(self)

    def complete_pager(self, text, line, bigidx, endidx):
        return ['on', 'off']

    def do_pager(self, line):
        if line == 'on':
            self.pager = True
        elif line == 'off':
            self.pager = False
        elif line == '':
            if self.pager:
                print('pager is on.')
            else:
                print('pager is off.')
        else:
            print('Argument error.')

    def do_shell(self, line):
        os.system(line)

    def complete_EOF(self, _text, _line, _begidx, _endidx):
        return []

    def do_EOF(self, _line):
        return True

    def do_exit(self, _line):
        return True

    def do_quit(self, _line):
        return True


class Configure(Cmd):

    def __init__(self, *args, **kwargs):
        self._in_onecmd = False
        self.prompt = 'Configure# '
        Cmd.__init__(self, *args, **kwargs)

    def do_set(self, line):
        """Add or modify candidate configuration line."""
        print('not implemented yet')

    def do_unset(self, line):
        """Remove candidate configuration line."""
        print('not implemented yet')

    def complete_show(self, text, line, bigidx, endidx):
        confdir = os.environ.get('HOME') + '/.lagopus.conf.d'
        params = os.listdir(confdir + '/')
        params.remove('.git')
        return [name for name in params if name.startswith(text)]

    def do_show(self, line):
        """Show the configuration.

        usage:
                show
                show file
        """
        confdir = os.environ.get('HOME') + '/.lagopus.conf.d'
        if not os.path.isdir(confdir):
            os.mkdir(confdir)
        args = line.split()
        try:
            conffile = confdir + '/' + args[0]
        except Exception:
            lines = ds_client().call('save\n').splitlines()
            self.output(dsl().decode(lines))
            return
        try:
            self.output(open(conffile).read())
        except Exception:
            print("failed to read " + conffile + "\n")

    def complete_commit(self, text, line, begidx, endidx):
        confdir = os.environ.get('HOME') + '/.lagopus.conf.d'
        params = os.listdir(confdir + '/')
        params.remove('.git')
        return [name for name in params if name.startswith(text)]

    def do_commit(self, line):
        """Commit configuration."""
        confdir = os.environ.get('HOME') + '/.lagopus.conf.d'
        args = line.split()
        try:
            if '/' in args[0]:
                conffile = args[0]
            else:
                conffile = confdir + '/' + args[0]
        except Exception:
            conffile = confdir + '/' + 'lagopus.conf'

        if not os.path.isfile(conffile):
            print('Do not exist configuration')
            return
        # Load candidate config and convert to DSL
        dslfile = conffile + '.dsl'
        f = open(dslfile, 'w')
        try:
            f.write(dsl().encode_file([conffile]))
        except DSLError as e:
            print(e.value)
            f.close()
            return
        f.close()
        # Load DSL.
        ds_client().call('destroy-all-obj\n')
        try:
            ds_client().call('load ' + dslfile + '\n')
        except DSLError as e:
            print(e.value)

    def do_load(self, line):
        """Load DSL

        Load configuration file and commit immediately.
        So far, use raw DSL output.
        """
        conffile = '@PREFIX@/etc/lagopus/lagopus.dsl'
        # Load candidate config and convert to DSL
        # Load DSL.
        ds_client().call('destroy-all-obj\n')
        try:
            ds_client().call('load ' + conffile + '\n')
        except DSLError as e:
            print(e.value)

    def do_save(self, line):
        """Save DSL

        Load configuration file and commit immediately.
        So far, use raw DSL output.
        """
        conffile = '@PREFIX@/etc/lagopus/lagopus.dsl'
        # Load candidate config and convert to DSL
        # Load DSL.
        ds_client().call('save ' + conffile + '\n')

    def complete_edit(self, text, line, bigidx, endidx):
        if inspect.currentframe().f_back.f_code.co_name != 'precmd':
            confdir = os.environ.get('HOME') + '/.lagopus.conf.d'
            params = os.listdir(confdir + '/')
            params.remove('.git')
            return [name for name in params if name.startswith(text)]
        else:
            return []

    def do_edit(self, line):
        """Edit candidate configuration.  Launch text editor."""
        confdir = os.environ.get('HOME') + '/.lagopus.conf.d'
        if not os.path.isdir(confdir):
            os.mkdir(confdir)
        if not os.path.isdir(confdir + '/.git'):
            os.system('cd ' + confdir + '; git init')
        args = line.split()
        try:
            conffile = confdir + '/' + args[0]
        except Exception:
            conffile = confdir + '/' + 'lagopus.conf'
            if not os.path.isfile(conffile):
                # save running DSL if candidate file is not exist.
                lines = ds_client().call('save\n').splitlines()
                f = open(conffile, 'w')
                f.write(dsl().decode(lines))
                f.close()
                # first git commit
                os.system('cd ' + confdir + '; git add ' + conffile)
                os.system('cd ' + confdir +
                          '; git commit --allow-empty-message -m ""')
        # Edit
        editor = os.environ.get('EDITOR', 'vi')
        try:
            st = os.system('cd ' + confdir + ';' + editor + ' ' + conffile)
            if st != 0:
                print('Aborted.')
                return
        except Exception:
            print('Execution failed.')
            return
        # git commit automatically
        os.system('cd ' + confdir + '; git add ' + conffile)
        os.system('cd ' + confdir + '; git commit --allow-empty-message -m ""')

    def do_ls(self, line):
        """Show configuration file contents."""
        confdir = os.environ.get('HOME') + '/.lagopus.conf.d'
        if not os.path.isdir(confdir):
            os.mkdir(confdir)
        os.system('cd ' + confdir + '; /bin/ls ' + line)

    def complete_diff(self, text, line, bigidx, endidx):
        confdir = os.environ.get('HOME') + '/.lagopus.conf.d'
        params = os.listdir(confdir + '/')
        params.remove('.git')
        return [name for name in params if name.startswith(text)]

    def do_diff(self, line):
        """Show differences from previous configuration."""
        confdir = os.environ.get('HOME') + '/.lagopus.conf.d'
        if not os.path.isdir(confdir):
            return
        if not os.path.isdir(confdir + '/.git'):
            return
        args = line.split()
        try:
            conffile = args[-1]
            if len(args) > 0 and os.path.isfile(confdir + '/' + conffile):
                args.pop(-1)
            else:
                raise
        except Exception:
            conffile = 'lagopus.conf'
            if not os.path.isfile(confdir + '/' + conffile):
                return
        if len(args) < 2:
            os.system('cd ' + confdir + '; git diff HEAD^ ' + conffile)
        else:
            os.system('cd ' + confdir + '; git diff ' +
                      ' '.join(map(str, args)) + ' ' + conffile)

    def do_history(self, line):
        """Show differences from previous configuration."""
        confdir = os.environ.get('HOME') + '/.lagopus.conf.d'
        if not os.path.isdir(confdir):
            return
        if not os.path.isdir(confdir + '/.git'):
            return
        args = line.split()
        try:
            conffile = args[0]
        except Exception:
            conffile = 'lagopus.conf'
            if not os.path.isfile(confdir + '/' + conffile):
                return
        os.system('cd ' + confdir + '; git log ' + conffile)


class Topcmd(Cmd):

    def __init__(self, *args, **kwargs):
        self._in_onecmd = False
        self.prompt = 'Lagosh> '
        Cmd.__init__(self, *args, **kwargs)

    def complete_configure(self, text, line, begidx, endidx):
        dotext = 'do_' + text
        return [a[3:] for a in Configure().get_names()
                if a.startswith(dotext) and a != 'do_shell']

    def do_configure(self, line):
        """Manipulate software configuration information."""
        if line == '':
            Configure().cmdloop(showpager=self.pager)
        else:
            Configure().onecmd(line)

    def complete_show(self, text, line, begidx, endidx):
        params = ['bridge', 'channel', 'controller', 'flow', 'group',
                  'mactable', 'interface', 'meter', 'port', 'route', 'version']
        return [name for name in params if name.startswith(text)]

    def subcmd_id_merge(self, subcmd, subcmd_id, res, resc):
        gstats = res[subcmd + 's']
        gconfig = resc[subcmd + 's']
        for gs in gstats:
            for gc in gconfig:
                if gs[subcmd + '-id'] == gc[subcmd + '-id']:
                    stats = gs[subcmd_id + '-stats']
                    config = gc[subcmd_id + 's']
                    for b in stats:
                        for c in config:
                            if c[subcmd_id + '-id'] == b[subcmd_id + '-id']:
                                b.update(c)

    def subcmd_show(self, line, key='bridge', stats='stats', subcmd_id=None):

        if len(line) == 0:
            print('Argument error')
            return

        args = line.split()
        subcmd = args[0]

        if len(args) >= 2:
            req = subcmd + ' ' + args[1] + ' ' + stats + '\n'
            try:
                res = ds_client().call(req)[0]
            except Exception:
                print('invalid keyword: ' + args[1])
                return
            if subcmd_id is not None:
                resc = ds_client().call(subcmd + ' ' + args[1] + '\n')[0]
                self.subcmd_id_merge(subcmd, subcmd_id, res, resc)
            if len(args) == 3:
                try:
                    res = res[args[2]]
                except Exception:
                    print('invalid keyword: ' + args[2])
                    return
            self.output(json.dumps(res, indent=4) + '\n')
        else:
            try:
                odata = []
                data = ds_client().call(key + '\n')
                for ifdata in data:
                    name = ifdata['name']
                    req = subcmd + ' ' + name.encode() + ' ' + stats + '\n'
                    res = ds_client().call(req)[0]
                    if subcmd_id is not None:
                        resc = ds_client().call((subcmd + ' '
                                                 + name.encode() + '\n'))[0]
                        self.subcmd_id_merge(subcmd, subcmd_id, res, resc)
                    try:
                        res['name'] = name
                        res['is-enabled'] = ifdata['is-enabled']
                    except Exception:
                        pass
                    odata.append(res)
                self.output(json.dumps(odata, indent=4) + '\n')
            except TypeError:
                return

    def do_show(self, line):
        """Show statistics.

        Usage
                show bridge
                show flow
                show mactable
                show interface
                show table
        """
        if len(line) == 0:
            print('Argument error')
            return

        args = line.split()
        subcmd = args[0]

        if subcmd == 'bridge':
            self.subcmd_show(line)

        elif subcmd == 'group':
            self.subcmd_show(line, 'bridge', 'stats', 'bucket')

        elif subcmd == 'meter':
            self.subcmd_show(line, 'bridge', 'stats', 'band')

        elif subcmd == 'flow':
            self.subcmd_show(line, 'bridge', '-with-stats')

        elif subcmd in ['interface', 'port']:
            self.subcmd_show(line, subcmd)

        elif subcmd == 'channel':
            data = ds_client().call('channel\n')
            self.output(json.dumps(data, indent=4))

        elif subcmd == 'controller':
            data = ds_client().call('controller\n')
            self.output(json.dumps(data, indent=4))

        elif subcmd == 'mactable':
            odata = []
            data = ds_client().call('bridge\n')
            try:
                for ifdata in data:
                    req = 'mactable ' + ifdata['name'].encode() + '\n'
                    res = ds_client().call(req)[0]
                    odata.append(res)
                self.output(json.dumps(odata, indent=4) + '\n')
            except Exception:
                return

        elif subcmd == 'route':
            data = ds_client().call('route\n')
            self.output(json.dumps(data, indent=4))

        elif subcmd == 'version':
            data = ds_client().call('version\n')
            self.output(json.dumps(data, indent=4))

        else:
            print('Argument error')

    def do_stop(self, line):
        """Stop lagopus process."""
        ds_client().call('shutdown\n', False)

    def do_log(self, line):
        """Show log settings."""
        data = ds_client().call('log\n')
        self.output(json.dumps(data, indent=4))

    def do_telnet(self, line):
        """Telnet to another host."""
        if len(line) == 0:
            print('Argument error')
            return

        args = line.split()
        host = args[0]
        os.system('telnet ' + host)

    def do_ssh(self, line):
        """Connect to another host with secure shell."""
        if len(line) == 0:
            print('Argument error')
            return

        args = line.split()
        host = args[0]
        os.system('ssh ' + host)

    def do_ping(self, line):
        """Ping a remote target."""
        if len(line) == 0:
            print('Argument error')
            return

        args = line.split()
        host = args[0]
        os.system('ping ' + host)

    def do_traceroute(self, line):
        """Trace the route to a remote host."""

        if len(line) == 0:
            print('Argument error')
            return
        args = line.split()
        host = args[0]
        os.system('traceroute ' + host)


def usage():
    print("Usage: " + sys.argv[0] + "[--dsl-decode [file]] "
          "[--dsl-encode [file]]")


def main():
    Topcmd().cmdloop()


if __name__ == "__main__":
    try:
        opts, args = getopt.getopt(sys.argv[1:],
                                   'p:c',
                                   ['dsl-decode=', 'dsl-encode='])
    except getopt.GetoptError:
        usage()
        sys.exit(2)
    for opt, arg in opts:
        if opt == '--dsl-decode':
            print(dsl().decode_file([arg]))
            sys.exit(0)
        elif opt == '--dsl-encode':
            try:
                print(dsl().encode_file([arg]))
            except DSLError as e:
                print(e.value)
            sys.exit(0)
        elif opt == '-p':
            ds_client.port = int(arg)
        elif opt == '-c':
            str = ''
            for word in args:
                str += word + ' '
            Topcmd().onecmd(str)
            sys.exit(0)
    main()
