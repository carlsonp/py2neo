#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2011-2013, Nigel Small
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" Command line swiss army knife for Neo4j.
"""


from __future__ import unicode_literals

import codecs
import json
import locale
import logging
import os
import readline
import sys

from . import __version__, __copyright__, neo4j, geoff
from .exceptions import CypherError
from .util import ustr
from .xmlutil import xml_to_cypher, xml_to_geoff


PY3 = sys.version > '3'
SCRIPT_NAME = "neotool"
NEOTOOL_HELP = """\
Usage:
  {script} <options> <command> <args>
Options:
  -h/--help [<command>]           Show tool usage
  -v/--version                    Show tool version
  -c/--copyright                  Show tool copyright
  -S/--scheme <scheme>            Set database scheme
  -H/--host <host>                Set database host
  -P/--port <port>                Set database port
  -U/--user <user>                Set HTTP basic auth user
  -W/--password <password>        Set HTTP basic auth password
Commands:
  clear                           Clear all nodes and relationships
  cypher <query>                  Execute Cypher and output as text
  cypher-csv <query>              Execute Cypher and output as CSV
  cypher-geoff <query>            Execute Cypher and output as Geoff
  cypher-json <query>             Execute Cypher and output as JSON
  cypher-tsv <query>              Execute Cypher and output as TSV
  geoff-insert <file>             Insert Geoff data
  geoff-merge <file>              Merge Geoff data
  shell                           Start an interactive shell
  xml-cypher <file> [<xmlns>...]  Convert XML data to Cypher CREATE statement
  xml-geoff <file> [<xmlns>...]   Convert XML data to Geoff data
"""
SHELL_HELP = """\
Enter Cypher or one of the following commands:
----
exit - exit the shell
help - display this help message
version - show the current Neo4j version
----
"""

if not PY3:
    _stdin = sys.stdin
    preferred_encoding = locale.getpreferredencoding()
    sys.stdin = codecs.getreader(preferred_encoding)(sys.stdin)
    sys.stdout = codecs.getwriter(preferred_encoding)(sys.stdout)
    sys.stderr = codecs.getwriter(preferred_encoding)(sys.stderr)


class ResultWriter(object):

    def __init__(self, out=None):
        self.out = out or sys.stdout

    @classmethod
    def _stringify(cls, value, quoted=False):
        if value is None:
            if quoted:
                return "null"
            else:
                return ""
        elif isinstance(value, list):
            out = " ".join(
                cls._stringify(item, quoted=False)
                for item in value
            )
            if quoted:
                out = json.dumps(out, separators=(',', ':'), ensure_ascii=False)
        else:
            if quoted:
                try:
                    out = json.dumps(value, ensure_ascii=False)
                except TypeError:
                    out = json.dumps(ustr(value), ensure_ascii=False)
            else:
                out = ustr(value)
        return out

    @classmethod
    def _jsonify(cls, value):
        if isinstance(value, list):
            out = "[" + ", ".join(cls._jsonify(i) for i in value) + "]"
        elif hasattr(value, "__uri__") and hasattr(value, "_properties"):
            metadata = {
                "uri": ustr(value.__uri__),
                "properties": value._properties,
            }
            try:
                metadata.update({
                    "start": ustr(value.start_node.__uri__),
                    "type": ustr(value.type),
                    "end": ustr(value.end_node.__uri__),
                })
            except AttributeError:
                pass
            out = json.dumps(metadata, ensure_ascii=False)
        else:
            out = json.dumps(value, ensure_ascii=False)
        return out

    def write_delimited(self, record_set, **kwargs):
        field_delimiter = kwargs.get("field_delimiter", "\t")
        self.out.write(field_delimiter.join([
            json.dumps(column, ensure_ascii=False)
            for column in record_set.columns
        ]))
        self.out.write("\n")
        for row in record_set:
            self.out.write(field_delimiter.join([
                ResultWriter._stringify(value, quoted=True)
                for value in row
            ]))
            self.out.write("\n")

    def write_geoff(self, record_set):
        nodes = set()
        rels = set()

        def update_descriptors(value):
            if isinstance(value, list):
                for item in value:
                    update_descriptors(item)
            elif hasattr(value, "__uri__") and hasattr(value, "_properties"):
                if hasattr(value, "type"):
                    rels.add(value)
                else:
                    nodes.add(value)

        for row in record_set:
            for i in range(len(row)):
                update_descriptors(row[i])
        for node in sorted(nodes):
            self.out.write(ustr(node))
            self.out.write("\n")
        for rel in sorted(rels):
            self.out.write(ustr(rel))
            self.out.write("\n")

    def write_json(self, record_set):
        columns = [json.dumps(column, ensure_ascii=False) for column in record_set.columns]
        row_count = 0
        self.out.write("[")
        for row in record_set:
            row_count += 1
            if row_count > 1:
                self.out.write(", ")
            self.out.write("{" + ", ".join([
                columns[i] + ": " + ResultWriter._jsonify(row[i])
                for i in range(len(row))
            ]) + "}")
        self.out.write("]")

    def write_text(self, record_set):
        columns = record_set.columns
        column_widths = [len(column) for column in columns]
        data = [
            [
                ResultWriter._stringify(value)
                for value in row
            ]
            for row in record_set
        ]
        for row in data:
            column_widths = [
                max(column_widths[i], len(value))
                for i, value in enumerate(row)
            ]
        self.out.write(" " + " | ".join(
            columns[i].ljust(column_widths[i])
            for i, column in enumerate(columns)
        ) + " \n")
        self.out.write("-" + "-+-".join(
            "".ljust(column_widths[i], "-")
            for i, column in enumerate(columns)
        ) + "-\n")
        for row in data:
            self.out.write(" " + " | ".join([
                value.ljust(column_widths[i])
                for i, value in enumerate(row)
            ]) + " \n")
        if len(data) == 1:
            self.out.write("(1 row)\n\n")
        else:
            self.out.write("({0} rows)\n\n".format(len(data)))

    formats = {
        "csv": (write_delimited, {"field_delimiter": ","}),
        "geoff": (write_geoff, {}),
        "json": (write_json, {}),
        "text": (write_text, {}),
        "tsv": (write_delimited, {"field_delimiter": "\t"}),
    }

    def write(self, format_, record_set):
        try:
            method, kwargs = self.formats[format_]
        except KeyError:
            raise ValueError("Unknown format {0}".format(repr(format_)))
        if kwargs:
            method(self, record_set, **kwargs)
        else:
            method(self, record_set)


class Tool(object):

    def __init__(self, in_=None, out=None, err=None):
        self._scheme = "http"
        self._host = "localhost"
        self._port = 7474
        self._user = None
        self._password = None
        self._in = in_ or sys.stdin
        self._out = out or sys.stdout
        self._err = err or sys.stderr
        self._script = None

    @property
    def _graph_db(self):
        host_port = "{0}:{1}".format(self._host, self._port)
        uri = "{0}://{1}".format(self._scheme, host_port)
        if self._user and self._password:
            neo4j.authenticate(host_port, self._user, self._password)
        return neo4j.ServiceRoot(uri).graph_db

    def _version(self):
        """ Show tool version
        """
        self._out.write("{0} (py2neo/{1})\n".format(SCRIPT_NAME, __version__))

    def _copyright(self):
        """ Show tool copyright
        """
        self._out.write("(C) Copyright {0}\n".format(__copyright__))

    def _help(self):
        """ Show tool usage
        """
        script = self._script.split(os.sep)[-1].rstrip(".py")
        self._version()
        self._out.write(NEOTOOL_HELP.format(script=script))
        self._copyright()

    def do(self, argv):
        self._script = argv.pop(0)
        command = None
        while not command:
            try:
                arg = argv.pop(0)
            except IndexError:
                self._help()
                sys.exit(0)
            if arg.startswith("-"):
                if arg in ("-h", "--help"):
                    self._help()
                    sys.exit(0)
                elif arg in ("-v", "--version"):
                    self._version()
                    sys.exit(0)
                elif arg in ("-c", "--copyright"):
                    self._copyright()
                    sys.exit(0)
                elif arg in ("-S", "--scheme"):
                    self._scheme = argv.pop(0)
                elif arg in ("-H", "--host"):
                    self._host = argv.pop(0)
                elif arg in ("-P", "--port"):
                    self._port = int(argv.pop(0))
                elif arg in ("-U", "--user"):
                    self._user = argv.pop(0)
                elif arg in ("-W", "--password"):
                    self._password = argv.pop(0)
                elif arg in ("-d", "--debug"):
                    logging.basicConfig(level=logging.DEBUG)
                elif arg in ("-i", "--info"):
                    logging.basicConfig(level=logging.INFO)
                else:
                    raise ValueError("Unknown option {0}".format(repr(arg)))
            else:
                command = arg
        try:
            method = getattr(self, command.replace("-", "_"))
        except AttributeError:
            raise ValueError("Unknown command {0}".format(repr(command)))
        args, kwargs = [], {}
        for arg in argv:
            if " " not in arg and "=" in arg:
                key, value = arg.partition("=")[0::2]
                kwargs[key] = value
            else:
                args.append(arg)
        try:
            method(*args, **kwargs)
        except TypeError as e:
            raise ValueError("Incorrect usage: {0}".format(e))

    def clear(self):
        """ Clear all nodes and relationships.
        """
        self._graph_db.clear()

    def _cypher(self, format_, query, params=None):
        if query == "-":
            query = self._in.read()
        record_set = neo4j.CypherQuery(self._graph_db, query).execute(**params or {})
        writer = ResultWriter(self._out)
        writer.write(format_, record_set)

    def cypher(self, query=None, **params):
        """ Execute Cypher query and output as text.
        """
        if not query:
            query = sys.stdin.read()
            sys.stdin.close()
        self._cypher("text", query, params)

    def cypher_csv(self, query=None, **params):
        """ Execute Cypher query and output as comma separated values
        """
        if not query:
            query = sys.stdin.read()
            sys.stdin.close()
        self._cypher("csv", query, params)

    def cypher_geoff(self, query=None, **params):
        """ Execute Cypher query and output as Geoff
        """
        if not query:
            query = sys.stdin.read()
            sys.stdin.close()
        self._cypher("geoff", query, params)

    def cypher_json(self, query=None, **params):
        """ Execute Cypher query and output as JSON
        """
        if not query:
            query = sys.stdin.read()
            sys.stdin.close()
        self._cypher("json", query, params)

    def cypher_tsv(self, query=None, **params):
        """ Execute Cypher query and output as tab separated values
        """
        if not query:
            query = sys.stdin.read()
            sys.stdin.close()
        self._cypher("tsv", query, params)

    def _geoff_write(self, params):
        for key, value in params.items():
            self._out.write(key)
            self._out.write("\t")
            self._out.write(ustr(value))
            self._out.write("\n")

    def geoff_insert(self, file_name=None):
        """ Insert Geoff data
        """
        if file_name:
            file = codecs.open(file_name, encoding="utf-8")
        else:
            file = sys.stdin
        params = geoff.Subgraph.load(file).insert_into(self._graph_db)
        self._geoff_write(params)

    def geoff_merge(self, file_name=None):
        """ Merge Geoff data
        """
        if file_name:
            file = codecs.open(file_name, encoding="utf-8")
        else:
            file = sys.stdin
        params = geoff.Subgraph.load(file).merge_into(self._graph_db)
        self._geoff_write(params)

    def xml_cypher(self, file_name=None, **prefixes):
        """ Convert XML data to Cypher CREATE statement.
        """
        if file_name:
            file = codecs.open(file_name, encoding="utf-8")
        else:
            file = sys.stdin
        src = file.read().encode("utf-8")
        file.close()
        self._out.write(xml_to_cypher(src, prefixes=prefixes))
        self._out.write("\n")

    def xml_geoff(self, file_name=None, **prefixes):
        """ Convert XML data to Geoff.
        """
        if file_name:
            file = codecs.open(file_name, encoding="utf-8")
        else:
            file = sys.stdin
        src = file.read().encode("utf-8")
        file.close()
        self._out.write(xml_to_geoff(src, prefixes=prefixes))
        self._out.write("\n")
        
    def shell(self):
        Shell(self._graph_db).repl()


class CommandLine(object):

    def __init__(self, text):
        self.text = text.strip()
    
    def __bool__(self):
        return bool(self.text)
    
    def __nonzero__(self):
        return bool(self.text)
    
    def peek(self):
        bits = self.text.split(None, 1)
        if not bits:
            return None
        return bits[0]
    
    def pop(self):
        bits = self.text.split(None, 1)
        if not bits:
            return None
        try:
            self.text = bits[1]
        except IndexError:
            self.text = ""
        return bits[0]


class Shell(object):

    def __init__(self, graph_db):
        self.graph_db = graph_db
        self.lang = "cypher"
        self.format = "text"
        self.param_sets = []

    @property
    def prompt(self):
        return "{0}/{1}[{2}]> ".format(self.graph_db.service_root.__uri__.host_port, self.lang, len(self.param_sets))

    def repl(self):
        print("Neotool Shell (py2neo/{0} Python/{1}.{2}.{3}-{4}-{5})".format(__version__, *sys.version_info))
        print("Copyright 2013, Nigel Small")
        print("")
        if PY3:
            self._repl3()
        else:
            sys.stdin = _stdin
            self._repl2()

    def _repl2(self):
        try:
            while True:
                line = raw_input(self.prompt).decode(sys.stdin.encoding)
                self.execute(line)
        except EOFError:
            print("⌁")
        except StopIteration:
            pass

    def _repl3(self):
        try:
            while True:
                line = input(self.prompt)
                self.execute(line)
        except EOFError:
            print("⌁")
        except StopIteration:
            pass

    def execute(self, line):
        line = CommandLine(line)
        if not line:
            return
        command = line.peek().upper()
        if command == "HELP":
            self.help(line)
        elif command == "EXECUTE":
            self.execute_cypher_from_file(line)
        elif command == "EXIT":
            raise StopIteration()
        elif command == "ADD":
            self.add_something(line)
        elif command == "CLEAR":
            self.clear_something(line)
        elif command == "SHOW":
            self.show_something(line)
        elif command == "VERSION":
            self.show_neo4j_version(line)
        else:
            self.execute_cypher(line.text, self.param_sets)

    def help(self, line):
        sys.stdout.write(SHELL_HELP)

    def execute_cypher(self, query, params):
        if isinstance(params, list):
            for p in params:
                self.execute_cypher(query, p)
        else:
            if not isinstance(params, dict):
                params = {}
            try:
                record_set = neo4j.CypherQuery(self.graph_db, query).execute(**params)
            except CypherError as err:
                sys.stderr.write("{0}: {1}".format(err.__class__.__name__, err))
                sys.stderr.write("\n")
            else:
                writer = ResultWriter(sys.stdout)
                writer.write(self.format, record_set)

    def execute_cypher_from_file(self, line):
        command = line.pop()
        file_name = os.path.expanduser(line.pop())
        try:
            with codecs.open(file_name, encoding="utf-8") as f:
                query = f.read()
        except IOError as err:
            sys.stderr.write("{0}: {1}".format(err.__class__.__name__, err))
            sys.stderr.write("\n")
        else:
            self.execute_cypher(query, self.params)
            
    def show_neo4j_version(self, line):
        print("Neo4j " + self.graph_db.__metadata__["neo4j_version"])

    def add_something(self, line):
        command = line.pop()
        subject = line.pop().upper()
        if subject == "PARAMETERS":
            self.add_parameters_from_file(line)
        else:
            sys.stderr.write("Bad command")

    def clear_something(self, line):
        command = line.pop()
        subject = line.pop().upper()
        if subject == "PARAMETERS":
            self.clear_parameters(line)
        else:
            sys.stderr.write("Bad command")

    def show_something(self, line):
        command = line.pop()
        subject = line.pop().upper()
        if subject == "PARAMETERS":
            self.show_parameters(line)
        else:
            sys.stderr.write("Bad command")

    def add_parameters_from_file(self, line):
        file_name = os.path.expanduser(line.pop())
        try:
            params = json.load(codecs.open(file_name, encoding="utf-8"))
        except IOError as err:
            sys.stderr.write("{0}: {1}".format(err.__class__.__name__, err))
            sys.stderr.write("\n")
        else:
            if isinstance(params, list):
                count = len(params)
                self.param_sets.extend(params)
            elif isinstance(self.params, dict):
                count = 1
                self.param_sets.append(params)
            else:
                count = 0
            if count == 1:
                print("1 parameter set added")
            else:
                print("{0} parameter sets added".format(count))

    def clear_parameters(self, line):
        self.param_sets = []

    def show_parameters(self, line):
        print(json.dumps(self.param_sets, sort_keys=True, indent=4))


if __name__ == "__main__":
    try:
        Tool().do(sys.argv)
        sys.exit(0)
    except Exception as err:
        sys.stderr.write(ustr(err))
        sys.stderr.write("\n")
        sys.exit(1)

