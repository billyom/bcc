#!/usr/bin/env python
#
# argdist   Trace a function and display a distribution of its
#              parameter values as a histogram or frequency count.
#
# USAGE: argdist [-h] [-p PID] [-z STRING_SIZE] [-i INTERVAL]
#                   [-n COUNT] [-v] [-T TOP]
#                   [-C specifier [specifier ...]]
#                   [-H specifier [specifier ...]]
#                   [-I header [header ...]]
#
# Licensed under the Apache License, Version 2.0 (the "License")
# Copyright (C) 2016 Sasha Goldshtein.

from bcc import BPF
from time import sleep, strftime
import argparse
import re
import traceback
import sys

class Specifier(object):
        probe_text = """
DATA_DECL

int PROBENAME(struct pt_regs *ctx SIGNATURE)
{
        PREFIX
        PID_FILTER
        if (!(FILTER)) return 0;
        KEY_EXPR
        COLLECT
        return 0;
}
"""
        next_probe_index = 0
        aliases = { "$PID": "bpf_get_current_pid_tgid()" }
        auto_includes = {
                "linux/time.h"    : ["time"],
                "linux/fs.h"      : ["fs", "file"],
                "linux/blkdev.h"  : ["bio", "request"],
                "linux/slab.h"    : ["alloc"]
        }

        @staticmethod
        def generate_auto_includes(specifiers):
                headers = ""
                for header, keywords in Specifier.auto_includes.items():
                        for keyword in keywords:
                                for specifier in specifiers:
                                        if keyword in specifier:
                                                headers += "#include <%s>\n" \
                                                           % header
                return headers

        def _substitute_aliases(self, expr):
                if expr is None:
                        return expr
                for alias, subst in Specifier.aliases.items():
                        expr = expr.replace(alias, subst)
                return expr

        def _parse_signature(self):
                params = map(str.strip, self.signature.split(','))
                self.param_types = {}
                for param in params:
                        # If the type is a pointer, the * can be next to the
                        # param name. Other complex types like arrays are not
                        # supported right now.
                        index = param.rfind('*')
                        index = index if index != -1 else param.rfind(' ')
                        param_type = param[0:index+1].strip()
                        param_name = param[index+1:].strip()
                        self.param_types[param_name] = param_type

        entry_probe_text = """
int PROBENAME(struct pt_regs *ctx SIGNATURE)
{
        u32 pid = bpf_get_current_pid_tgid();
        PID_FILTER
        COLLECT
        return 0;
}
"""

        def _generate_entry(self):
                self.entry_probe_func = self.probe_func_name + "_entry"
                text = self.entry_probe_text
                text = text.replace("PROBENAME", self.entry_probe_func)
                text = text.replace("SIGNATURE",
                     "" if len(self.signature) == 0 else ", " + self.signature)
                pid_filter = "" if self.is_user or self.pid is None \
                                else "if (pid != %d) { return 0; }" % self.pid
                text = text.replace("PID_FILTER", pid_filter)
                collect = ""
                for pname in self.args_to_probe:
                        param_hash = self.hashname_prefix + pname
                        if pname == "__latency":
                                collect += """
u64 __time = bpf_ktime_get_ns();
%s.update(&pid, &__time);
"""                             % param_hash
                        else:
                                collect += "%s.update(&pid, &%s);\n" % \
                                           (param_hash, pname)
                text = text.replace("COLLECT", collect)
                return text

        def _generate_entry_probe(self):
                # Any $entry(name) expressions result in saving that argument
                # when entering the function.
                self.args_to_probe = set()
                regex = r"\$entry\((\w+)\)"
                for expr in self.exprs:
                        for arg in re.finditer(regex, expr):
                                self.args_to_probe.add(arg.group(1))
                for arg in re.finditer(regex, self.filter):
                        self.args_to_probe.add(arg.group(1))
                if any(map(lambda expr: "$latency" in expr, self.exprs)) or \
                   "$latency" in self.filter:
                        self.args_to_probe.add("__latency")
                        self.param_types["__latency"] = "u64"    # nanoseconds
                for pname in self.args_to_probe:
                        if pname not in self.param_types:
                                raise ValueError("$entry(%s): no such param" \
                                                % arg)

                self.hashname_prefix = "%s_param_" % self.probe_hash_name
                text = ""
                for pname in self.args_to_probe:
                        # Each argument is stored in a separate hash that is
                        # keyed by pid.
                        text += "BPF_HASH(%s, u32, %s);\n" % \
                             (self.hashname_prefix + pname,
                              self.param_types[pname])
                text += self._generate_entry()
                return text

        def _generate_retprobe_prefix(self):
                # After we're done here, there are __%s_val variables for each
                # argument we needed to probe using $entry(name), and they all
                # have values (which isn't necessarily the case if we missed
                # the method entry probe).
                text = "u32 __pid = bpf_get_current_pid_tgid();\n"
                self.param_val_names = {}
                for pname in self.args_to_probe:
                        val_name = "__%s_val" % pname
                        text += "%s *%s = %s.lookup(&__pid);\n" % \
                                (self.param_types[pname], val_name,
                                 self.hashname_prefix + pname)
                        text += "if (%s == 0) { return 0 ; }\n" % val_name
                        self.param_val_names[pname] = val_name
                return text

        def _replace_entry_exprs(self):
                for pname, vname in self.param_val_names.items():
                        if pname == "__latency":
                                entry_expr = "$latency"
                                val_expr = "(bpf_ktime_get_ns() - *%s)" % vname
                        else:
                                entry_expr = "$entry(%s)" % pname
                                val_expr = "(*%s)" % vname
                        for i in range(0, len(self.exprs)):
                                self.exprs[i] = self.exprs[i].replace(
                                                entry_expr, val_expr)
                        self.filter = self.filter.replace(entry_expr,
                                                          val_expr)

        def _attach_entry_probe(self):
                if self.is_user:
                        self.bpf.attach_uprobe(name=self.library,
                                               sym=self.function,
                                               fn_name=self.entry_probe_func,
                                               pid=self.pid or -1)
                else:
                        self.bpf.attach_kprobe(event=self.function,
                                               fn_name=self.entry_probe_func)

        def _bail(self, error):
                raise ValueError("error parsing probe '%s': %s" %
                                 (self.raw_spec, error))

        def _validate_specifier(self):
                # Everything after '#' is the probe label, ignore it
                spec = self.raw_spec.split('#')[0]
                parts = spec.strip().split(':')
                if len(parts) < 3:
                        self._bail("at least the probe type, library, and " +
                                   "function signature must be specified")
                if len(parts) > 6:
                        self._bail("extraneous ':'-separated parts detected")
                if parts[0] not in ["r", "p"]:
                        self._bail("probe type must be either 'p' or 'r', " +
                                   "but got '%s'" % parts[0])
                if re.match(r"\w+\(.*\)", parts[2]) is None:
                        self._bail(("function signature '%s' has an invalid " +
                                    "format") % parts[2])

        def _parse_expr_types(self, expr_types):
                if len(expr_types) == 0:
                        self._bail("no expr types specified")
                self.expr_types = expr_types.split(',')

        def _parse_exprs(self, exprs):
                if len(exprs) == 0:
                        self._bail("no exprs specified")
                self.exprs = exprs.split(',')

        def __init__(self, type, specifier, pid):
                self.raw_spec = specifier
                self._validate_specifier()

                spec_and_label = specifier.split('#')
                self.label = spec_and_label[1] \
                             if len(spec_and_label) == 2 else None

                parts = spec_and_label[0].strip().split(':')
                self.type = type    # hist or freq
                self.is_ret_probe = parts[0] == "r"
                self.library = parts[1]
                self.is_user = len(self.library) > 0
                fparts = parts[2].split('(')
                self.function = fparts[0].strip()
                self.signature = fparts[1].strip()[:-1]
                self._parse_signature()

                # If the user didn't specify an expression to probe, we probe
                # the retval in a ret probe, or simply the value "1" otherwise.
                self.is_default_expr = len(parts) < 5
                if not self.is_default_expr:
                        self._parse_expr_types(parts[3])
                        self._parse_exprs(parts[4])
                        if len(self.exprs) != len(self.expr_types):
                                self._bail("mismatched # of exprs and types")
                        if self.type == "hist" and len(self.expr_types) > 1:
                                self._bail("histograms can only have 1 expr")
                else:
                        if not self.is_ret_probe and self.type == "hist":
                                self._bail("histograms must have expr")
                        self.expr_types = \
                                ["u64" if not self.is_ret_probe else "int"]
                        self.exprs = \
                                ["1" if not self.is_ret_probe else "$retval"]
                self.filter = "" if len(parts) != 6 else parts[5]
                self._substitute_exprs()

                # Do we need to attach an entry probe so that we can collect an
                # argument that is required for an exit (return) probe?
                def check(expr):
                        keywords = ["$entry", "$latency"]
                        return any(map(lambda kw: kw in expr, keywords))
                self.entry_probe_required = self.is_ret_probe and \
                        (any(map(check, self.exprs)) or check(self.filter))

                self.pid = pid
                self.probe_func_name = "%s_probe%d" % \
                        (self.function, Specifier.next_probe_index)
                self.probe_hash_name = "%s_hash%d" % \
                        (self.function, Specifier.next_probe_index)
                Specifier.next_probe_index += 1

        def _substitute_exprs(self):
                def repl(expr):
                        expr = self._substitute_aliases(expr)
                        return expr.replace("$retval", "ctx->ax")
                for i in range(0, len(self.exprs)):
                        self.exprs[i] = repl(self.exprs[i])
                self.filter = repl(self.filter)

        def _is_string(self, expr_type):
                return expr_type == "char*" or expr_type == "char *"

        def _generate_hash_field(self, i):
                if self._is_string(self.expr_types[i]):
                        return "struct __string_t v%d;\n" % i
                else:
                        return "%s v%d;\n" % (self.expr_types[i], i)

        def _generate_field_assignment(self, i):
                if self._is_string(self.expr_types[i]):
                        return "bpf_probe_read(" + \
                               "&__key.v%d.s, sizeof(__key.v%d.s), %s);\n" % \
                                (i, i, self.exprs[i])
                else:
                        return "__key.v%d = %s;\n" % (i, self.exprs[i])

        def _generate_hash_decl(self):
                if self.type == "hist":
                        return "BPF_HISTOGRAM(%s, %s);" % \
                               (self.probe_hash_name, self.expr_types[0])
                else:
                        text = "struct %s_key_t {\n" % self.probe_hash_name
                        for i in range(0, len(self.expr_types)):
                                text += self._generate_hash_field(i)
                        text += "};\n"
                        text += "BPF_HASH(%s, struct %s_key_t, u64);\n" % \
                                (self.probe_hash_name, self.probe_hash_name)
                        return text

        def _generate_key_assignment(self):
                if self.type == "hist":
                        return "%s __key = %s;\n" % \
                                (self.expr_types[0], self.exprs[0])
                else:
                        text = "struct %s_key_t __key = {};\n" % \
                                self.probe_hash_name
                        for i in range(0, len(self.exprs)):
                                text += self._generate_field_assignment(i)
                        return text

        def _generate_hash_update(self):
                if self.type == "hist":
                        return "%s.increment(bpf_log2l(__key));" % \
                                self.probe_hash_name
                else:
                        return "%s.increment(__key);" % self.probe_hash_name

        def _generate_pid_filter(self):
                # Kernel probes need to explicitly filter pid, because the
                # attach interface doesn't support pid filtering
                if self.pid is not None and not self.is_user:
                        return "u32 pid = bpf_get_current_pid_tgid();\n" + \
                               "if (pid != %d) { return 0; }" % self.pid
                else:
                        return ""

        def generate_text(self):
                # We don't like tools writing tools (Brendan Gregg), but this
                # is an exception because we're letting the user fully
                # customize the values we probe. As a rule of thumb though,
                # try to build a custom tool for a specific purpose.

                program = ""

                # If any entry arguments are probed in a ret probe, we need
                # to generate an entry probe to collect them
                prefix = ""
                if self.entry_probe_required:
                        program = self._generate_entry_probe()
                        prefix = self._generate_retprobe_prefix()
                        # Replace $entry(paramname) with a reference to the
                        # value we collected when entering the function:
                        self._replace_entry_exprs()

                program += self.probe_text.replace("PROBENAME",
                                                   self.probe_func_name)
                signature = "" if len(self.signature) == 0 \
                                  or self.is_ret_probe \
                               else ", " + self.signature
                program = program.replace("SIGNATURE", signature)
                program = program.replace("PID_FILTER",
                                          self._generate_pid_filter())

                decl = self._generate_hash_decl()
                key_expr = self._generate_key_assignment()
                collect = self._generate_hash_update()
                program = program.replace("DATA_DECL", decl)
                program = program.replace("KEY_EXPR", key_expr)
                program = program.replace("FILTER",
                        "1" if len(self.filter) == 0 else self.filter)
                program = program.replace("COLLECT", collect)
                program = program.replace("PREFIX", prefix)
                return program

        def attach(self, bpf):
                self.bpf = bpf
                if self.is_user:
                        if self.is_ret_probe:
                                bpf.attach_uretprobe(name=self.library,
                                                  sym=self.function,
                                                  fn_name=self.probe_func_name,
                                                  pid=self.pid or -1)
                        else:
                                bpf.attach_uprobe(name=self.library,
                                                  sym=self.function,
                                                  fn_name=self.probe_func_name,
                                                  pid=self.pid or -1)
                else:
                        if self.is_ret_probe:
                                bpf.attach_kretprobe(event=self.function,
                                                  fn_name=self.probe_func_name)
                        else:
                                bpf.attach_kprobe(event=self.function,
                                                  fn_name=self.probe_func_name)
                if self.entry_probe_required:
                        self._attach_entry_probe()

        def _v2s(self, v):
                # Most fields can be converted with plain str(), but strings
                # are wrapped in a __string_t which has an .s field
                if "__string_t" in type(v).__name__:
                        return str(v.s)
                return str(v)

        def _display_expr(self, i):
                # Replace ugly latency calculation with $latency
                expr = self.exprs[i].replace(
                        "(bpf_ktime_get_ns() - *____latency_val)", "$latency")
                # Replace alias values back with the alias name
                for alias, subst in Specifier.aliases.items():
                        expr = expr.replace(subst, alias)
                # Replace retval expression with $retval
                expr = expr.replace("ctx->ax", "$retval")
                # Replace ugly (*__param_val) expressions with param name
                return re.sub(r"\(\*__(\w+)_val\)", r"\1", expr)

        def _display_key(self, key):
                if self.is_default_expr:
                        if not self.is_ret_probe:
                                return "total calls"
                        else:
                                return "retval = %s" % str(key.v0)
                else:
                        # The key object has v0, ..., vk fields containing
                        # the values of the expressions from self.exprs
                        def str_i(i):
                                key_i = self._v2s(getattr(key, "v%d" % i))
                                return "%s = %s" % \
                                        (self._display_expr(i), key_i)
                        return ", ".join(map(str_i, range(0, len(self.exprs))))

        def display(self, top):
                data = self.bpf.get_table(self.probe_hash_name)
                if self.type == "freq":
                        print(self.label or self.raw_spec)
                        print("\t%-10s %s" % ("COUNT", "EVENT"))
                        data = sorted(data.items(), key=lambda kv: kv[1].value)
                        if top is not None:
                                data = data[-top:]
                        for key, value in data:
                                # Print some nice values if the user didn't
                                # specify an expression to probe
                                if self.is_default_expr:
                                        if not self.is_ret_probe:
                                                key_str = "total calls"
                                        else:
                                                key_str = "retval = %s" % \
                                                          self._v2s(key.v0)
                                else:
                                        key_str = self._display_key(key)
                                print("\t%-10s %s" % \
                                      (str(value.value), key_str))
                elif self.type == "hist":
                        label = self.label or (self._display_expr(0)
                                if not self.is_default_expr  else "retval")
                        data.print_log2_hist(val_type=label)

class Tool(object):
        examples = """
Probe specifier syntax:
        {p,r}:[library]:function(signature)[:type[,type...]:expr[,expr...][:filter]][#label]
Where:
        p,r        -- probe at function entry or at function exit
                      in exit probes: can use $retval, $entry(param), $latency
        library    -- the library that contains the function
                      (leave empty for kernel functions)
        function   -- the function name to trace
        signature  -- the function's parameters, as in the C header
        type       -- the type of the expression to collect (supports multiple)
        expr       -- the expression to collect (supports multiple)
        filter     -- the filter that is applied to collected values
        label      -- the label for this probe in the resulting output

EXAMPLES:

argdist -H 'p::__kmalloc(u64 size):u64:size'
        Print a histogram of allocation sizes passed to kmalloc

argdist -p 1005 -C 'p:c:malloc(size_t size):size_t:size:size==16'
        Print a frequency count of how many times process 1005 called malloc
        with an allocation size of 16 bytes

argdist -C 'r:c:gets():char*:(char*)$retval#snooped strings'
        Snoop on all strings returned by gets()

argdist -H 'r::__kmalloc(size_t size):u64:$latency/$entry(size)#ns per byte'
        Print a histogram of nanoseconds per byte from kmalloc allocations

argdist -C 'p::__kmalloc(size_t size, gfp_t flags):size_t:size:flags&GFP_ATOMIC'
        Print frequency count of kmalloc allocation sizes that have GFP_ATOMIC

argdist -p 1005 -C 'p:c:write(int fd):int:fd' -T 5
        Print frequency counts of how many times writes were issued to a
        particular file descriptor number, in process 1005, but only show
        the top 5 busiest fds

argdist -p 1005 -H 'r:c:read()'
        Print a histogram of results (sizes) returned by read() in process 1005

argdist -C 'r::__vfs_read():u32:$PID:$latency > 100000'
        Print frequency of reads by process where the latency was >0.1ms

argdist -H 'r::__vfs_read(void *file, void *buf, size_t count):size_t:$entry(count):$latency > 1000000'
        Print a histogram of read sizes that were longer than 1ms

argdist -H \\
        'p:c:write(int fd, const void *buf, size_t count):size_t:count:fd==1'
        Print a histogram of buffer sizes passed to write() across all
        processes, where the file descriptor was 1 (STDOUT)

argdist -C 'p:c:fork()#fork calls'
        Count fork() calls in libc across all processes
        Can also use funccount.py, which is easier and more flexible

argdist  -H \\
        'p:c:sleep(u32 seconds):u32:seconds' \\
        'p:c:nanosleep(struct timespec *req):long:req->tv_nsec'
        Print histograms of sleep() and nanosleep() parameter values

argdist -p 2780 -z 120 \\
        -C 'p:c:write(int fd, char* buf, size_t len):char*:buf:fd==1'
        Spy on writes to STDOUT performed by process 2780, up to a string size
        of 120 characters
"""

        def __init__(self):
                parser = argparse.ArgumentParser(description="Trace a " +
                  "function and display a summary of its parameter values.",
                  formatter_class=argparse.RawDescriptionHelpFormatter,
                  epilog=Tool.examples)
                parser.add_argument("-p", "--pid", type=int,
                  help="id of the process to trace (optional)")
                parser.add_argument("-z", "--string-size", default=80,
                  type=int,
                  help="maximum string size to read from char* arguments")
                parser.add_argument("-i", "--interval", default=1, type=int,
                  help="output interval, in seconds")
                parser.add_argument("-n", "--number", type=int, dest="count",
                  help="number of outputs")
                parser.add_argument("-v", "--verbose", action="store_true",
                  help="print resulting BPF program code before executing")
                parser.add_argument("-T", "--top", type=int,
                  help="number of top results to show (not applicable to " +
                  "histograms)")
                parser.add_argument("-H", "--histogram", nargs="*",
                  dest="histspecifier", metavar="specifier",
                  help="probe specifier to capture histogram of " +
                  "(see examples below)")
                parser.add_argument("-C", "--count", nargs="*",
                  dest="countspecifier", metavar="specifier",
                  help="probe specifier to capture count of " +
                  "(see examples below)")
                parser.add_argument("-I", "--include", nargs="*",
                  metavar="header",
                  help="additional header files to include in the BPF program")
                self.args = parser.parse_args()

        def _create_specifiers(self):
                self.specifiers = []
                for specifier in (self.args.countspecifier or []):
                        self.specifiers.append(Specifier(
                                "freq", specifier, self.args.pid))
                for histspecifier in (self.args.histspecifier or []):
                        self.specifiers.append(
                                Specifier("hist", histspecifier, self.args.pid))
                if len(self.specifiers) == 0:
                        print("at least one specifier is required")
                        exit(1)

        def _generate_program(self):
                bpf_source = """
struct __string_t { char s[%d]; };

#include <uapi/linux/ptrace.h>
                """ % self.args.string_size
                for include in (self.args.include or []):
                        bpf_source += "#include <%s>\n" % include
                bpf_source += Specifier.generate_auto_includes(
                                map(lambda s: s.raw_spec, self.specifiers))
                for specifier in self.specifiers:
                        bpf_source += specifier.generate_text()
                if self.args.verbose:
                        print(bpf_source)
                self.bpf = BPF(text=bpf_source)

        def _attach(self):
                for specifier in self.specifiers:
                        specifier.attach(self.bpf)

        def _main_loop(self):
                count_so_far = 0
                while True:
                        try:
                                sleep(self.args.interval)
                        except KeyboardInterrupt:
                                exit()
                        print("[%s]" % strftime("%H:%M:%S"))
                        for specifier in self.specifiers:
                                specifier.display(self.args.top)
                        count_so_far += 1
                        if self.args.count is not None and \
                           count_so_far >= self.args.count:
                                exit()

        def run(self):
                try:
                        self._create_specifiers()
                        self._generate_program()
                        self._attach()
                        self._main_loop()
                except:
                        if self.args.verbose:
                                traceback.print_exc()
                        else:
                                print(sys.exc_value)

if __name__ == "__main__":
        Tool().run()
