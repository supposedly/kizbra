"""
argparse sucks
this sucks too but less
"""
import os
import sys
import shlex
from functools import partial
from itertools import chain, zip_longest, starmap

from . import errors, _private
from .clumps import And, Or, Xor, ClumpGroup
from .entity import Entity, Arg, Flag
from .misc import ErgoNamespace, _Null


_FILE = os.path.basename(sys.argv[0])


class HelperMixin:
    @property
    def all_commands(self):
        return {*self.commands, *(name for g in self._groups for name in g.commands)}
    
    @property
    def all_flags(self):
        return self.flags.values()
    
    @property
    def all_args(self):
        return map(self.getarg, self.args)
    
    @property
    def usage_info(self):
        return '{}{} {} {}'.format(
          _FILE,
          str(self),
          ' '.join(map(str, self.all_flags)),
          ' '.join(map(str, self.all_args)),
          )
    
    @property
    def help_info(self):
        return '\n'.join(
          '{}\n{}'.format(
            label.upper(),
            '\n'.join({
              '\t{: <15} {}'.format(i.name, i.brief)
              for i in getattr(self, 'all_' + label)
            }),
          )
          for label in ('args', 'flags')
        )
    
    def format_help(self, usage=True, commands=True, help=True):
        built = ['', self.desc, ''] if self.desc else ['']
        if usage:
            built.append('usage: {}'.format(self.usage_info))
        if commands and self.commands:
            built.append('commands: {}'.format(', '.join(map(str, self.all_commands))))
        if help and (usage or commands):
            built.append('')
        if help:
            built.append(self.help_info)
        return '\n'.join(built)
    
    def print_help(self, usage=True, commands=True, help=True):
        print(self.format_help(usage, commands, help), end='\n\n')
    
    def error(self, exc=None, help=True):
        self.print_help(help=help)
        if exc is None:
            raise SystemExit
        raise SystemExit(exc if str(exc) else type(exc))
    
    def cli_help(self, name=None):
        if name is None:
            self.error()
        
        entity = self.get(name)
        if entity is None:
            print('No helpable entity named', repr(name))
            raise SystemExit
        
        try:
            short = getattr(entity, 'short', '')
            aliases = ', '.join(map(repr, (k for k, v in self._aliases.items() if v == name and k != short)))
        except AttributeError:
            aliases = ''
        
        if aliases:
            print('', entity, 'Aliases: {}\n'.format(aliases), entity.help, sep='\n', end='\n\n')
        else:
            print('', entity, entity.help, sep='\n', end='\n\n')
        raise SystemExit


class _Handler:
    def __init__(self):
        self.arg_map = {}
        self.commands = {}
        self.flags = {}
        self._aliases = {}
        self._defaults = {}
        
        self.args = []
        self._last_arg_consumes = False
        
        self._and = ClumpGroup()
        self._or = ClumpGroup()
        self._xor = ClumpGroup()
        
        self._required = set()
    
    def __repr__(self):
        quote = "'" if hasattr(self, 'name') else ''
        return '<{}{s}{q}{}{q}>'.format(
          self.__class__.__name__,
          getattr(self, 'name', ''),
          s=' ' if hasattr(self, 'name') else '',
          q=quote,
          )
    
    @property
    def defaults(self):
        return ErgoNamespace(**self._defaults, **{cmd.name: cmd.defaults for cmd in self.commands.values()})
    
    @property
    def parent_and(self):
        return self._and
    
    @property
    def parent_or(self):
        return self._or
    
    @property
    def parent_xor(self):
        return self._xor
    
    @property
    def entity_names(self):
        return set(chain(self.arg_map, self.commands, self.flags))
    
    def dealias(self, name):
        return self._aliases.get(name, name)
    
    def remove(self, obj):
        name = obj.identifier if isinstance(obj, Entity.cls) else obj
        if name in self.arg_map:
            del self.arg_map[name]
            self.args = list(filter(name.__eq__, self.args))
        else:
            try:
                del next(filter(lambda c: name in c, (self.commands, self.flags)))[name]
            except StopIteration:
                raise KeyError('No such entity: {}'.format(obj))
        self._aliases = {k: v for k, v in self._aliases.items() if v != name}
    
    def get(self, name):
        for func in (self.getarg, self.getflag, self.getcmd):
            try:
                return func(name)
            except KeyError:
                pass
        return None
    
    def getarg(self, name):
        try:
            return self.arg_map[name]
        except KeyError:
            return self._aliases[self.arg_map[name]]
    
    def getflag(self, name):
        try:
            return self.flags[name]
        except KeyError:
            return self.flags[self._aliases[name]]
    
    def getcmd(self, name):
        try:
            return self.commands[name]
        except KeyError:
            return self.commands[self._aliases[name]]
    
    def hasflag(self, name):
        return name in self.flags or self._aliases.get(name, _Null) in self.flags
    
    def hascmd(self, name):
        return name in self.commands or self._aliases.get(name, _Null) in self.commands
    
    def hasany(self, name):
        return self.hasflag(name) or self.hascmd(name) or name in self.arg_map or self._aliases.get(name, _Null) in self.arg_map
    
    def _clump(self, obj, AND, OR, XOR):
        if AND is not _Null:
            self._and.add(And(AND, self))
            And(AND, self).add(obj)
        if OR is not _Null:
            self._or.add(Or(OR, self))
            Or(OR, self).add(obj)
        if XOR is not _Null:
            self._xor.add(Xor(XOR, self))
            Xor(XOR, self).add(obj)
    
    def enforce_clumps(self, parsed, groups=None):
        elim = {lbl.upper(): getattr(self, 'parent_'+lbl).successes(parsed) for lbl in ('and', 'or', 'xor')}
        if groups is not None:
            g_clumps = {lbl: {groups.get(i) for i in set(successes).intersection(groups)} for lbl, successes in elim.items()}
            zipped = {lbl: (elim[lbl], {name for g in groups for name in g.entity_names}) for lbl, groups in g_clumps.items()}
            elim = {lbl: grp.union(success) for lbl, (success, grp) in zipped.items()}
        
        def extract_names(collection):
            if groups is None:
                return map(repr, collection)
            return (
              '[{}]'.format(
                ', '.join(map(repr, groups[n].entity_names))
                )
              if n in groups
              else repr(n)
              for n in collection
              )
        
        err_details = {
          'AND_SUC': elim['AND'],
          'OR_SUC': elim['OR'],
          'XOR_SUC': elim['XOR'],
          'parsed': parsed,
          'groups': groups,
          'handler': repr(self)
          }
        
        for all_failed, received in self._and.failures(parsed):
            # AND failure == member of an AND clump that was not given
            # an AND failure is okay if it's in a satisfied OR clump (i.e. there's at least one other OR in its clump that was given)
            # or if it's in a satisfied XOR clump (i.e. exactly one other XOR in its clump was given)
            not_exempt = (all_failed - received) - elim['OR'] - elim['XOR']
            if not_exempt:
                raise errors.ANDError(
                  'Expected all of the following flags/arguments: {}\n(Got {})'.format(
                      ', '.join(extract_names(all_failed)),
                      ', '.join(extract_names(received)) or 'none'
                    ),
                  **err_details,
                  failed=all_failed, eliminating=received, not_exempt=not_exempt
                  )
        
        for all_failed, received in self._or.failures(parsed):
            # OR failure == member of an OR clump where none were given
            # an OR failure is okay if it's in a satisfied XOR clump (i.e. exactly one other XOR in its clump was given)
            not_exempt = (all_failed - received) - elim['XOR']
            if not_exempt:
                raise errors.ORError(
                  'Expected at least one of the following flags/arguments: {}\n(Got none)'.format(
                      ', '.join(extract_names(all_failed))
                    ),
                  **err_details,
                  failed=all_failed, eliminating=received, not_exempt=not_exempt
                  )
        
        for all_failed, not_received in self._xor.failures(parsed):
            # XOR failure == member of an XOR clump that was given alongside at least one other
            # an XOR failure is okay if it satisfies an AND clump (i.e. all other ANDs in its clump were given)
            ####################################################################################
            # XXX: SHOULD it be okay if it satisfies an OR clump? What about if it's required? #
            # That is: should this actually say `- elim['AND'] - elim['OR'] - self._required`? #
            ####################################################################################
            not_exempt = (all_failed - not_received) - elim['AND']
            if len(not_exempt) > 1:
                raise errors.XORError(
                  'Expected no more than one of the following flags/arguments: {}\n(Got {})'.format(
                      ', '.join(extract_names(all_failed)),
                      ', '.join(extract_names(all_failed-not_received))
                    ),
                  **err_details,
                  failed=all_failed, eliminating=not_received, not_exempt=not_exempt
                  )
        return True
    
    def clump(self, *, AND=_Null, OR=_Null, XOR=_Null):
        def inner(cb):
            entity = Entity(cb.func if isinstance(cb, Entity.cls) else cb)
            self._clump(entity, AND, OR, XOR)
            return entity
        return inner
    
    def arg(self, n=1, *, required=False, default=_Null, namespace=None, help=None):
        """
        n: number of times this arg should be received consecutively; ... for infinite
        Expected kwargs: _ (str), help (str)
        """
        def inner(cb):
            repeat_count = n
            entity = Arg(cb, n, namespace=namespace, help=help)
            self.arg_map[entity.name] = entity
            if required:
                self._required.add(entity.name)
            if repeat_count is Ellipsis:
                self._last_arg_consumes = True
                repeat_count = 1
            if default is not _Null:
                self._defaults[entity.name] = default
            self.args.extend([entity.name] * repeat_count)
            return entity
        return inner
    
    def flag(self, dest=None, short=_Null, *, aliases=(), default=_Null, required=False, namespace=None, help=None, _='-'):
        def inner(cb):
            entity = Flag(cb, namespace=namespace, name=dest, help=help, _=_)
            # filter out '<lambda>'
            if cb.__name__.isidentifier() and dest is not None:
                self._aliases[cb.__name__] = entity.name
            if short is not None:  # _Null == default; None == none
                try:
                    entity.short = short or next(s for s in entity.name if s.isalnum() and s not in self._aliases)
                except StopIteration:
                    pass
                else:
                    self._aliases[entity.short] = entity.name
            if default is not _Null:
                self._defaults[entity.identifier] = default
            if required:
                self._required.add(entity.name)
            for alias in aliases:
                self._aliases[alias] = entity.name
            self.flags[entity.name] = entity
            return entity
        return inner
    
    def command(self, name, desc='', *args, AND=_Null, OR=_Null, XOR=_Null, from_cli=None, aliases=(), _='-', **kwargs):
        if from_cli is None:
            subcmd = Command(*args, **kwargs, name=name, desc=desc, parent=self)
        else:
            subcmd = Command.from_cli(from_cli, self, name)
        visual_name = name.replace('_', _)
        for alias in aliases:
            self._aliases[alias] = visual_name
        self._clump(subcmd, AND, OR, XOR)
        self.commands[visual_name] = subcmd
        return subcmd


class ParserBase(_Handler, HelperMixin):
    def __init__(self, desc='', flag_prefix='-', *, systemexit=True, no_help=False, default_command=None):
        self.desc = desc
        self.flag_prefix = flag_prefix
        self.long_prefix = 2 * flag_prefix
        self.default_command = default_command
        self.systemexit = systemexit
        self._groups = set()
        super().__init__()
        if not flag_prefix:
            raise ValueError('Flag prefix cannot be empty')
        if not no_help:
            self.flag(
              'help',
              help="Prints help and exits\nIf given a valid NAME, displays that entity's help"
              )(lambda name=None: self.cli_help(name))
    
    def __setattr__(self, name, val):
        if not isinstance(val, Group):
            return object.__setattr__(self, name, val)
        
        if name in vars(self):
            raise ValueError('Group name already in use for this cli: ' + name)
        if val._required:
            self._required.add(name)
        
        self._groups.add(val)
        self._clump(val, val._and, val._or, val._xor)
        
        SubHandler.__init__(val, self, name)
        object.__setattr__(self, name, val)
    
    @property
    def defaults(self):
        return super().defaults._.set_default_key(self.default_command)
    
    def dealias(self, name):
        try:
            return next(g.dealias(name) for g in self._groups if g.hasany(name))
        except StopIteration:
            return super().dealias(name)
    
    def remove(self, obj):
        name = obj.name if isinstance(obj, Entity.cls) else obj
        for g in self._groups:
            try:
                return g.remove(name)
            except KeyError:
                pass
        return super().remove(name)
    
    def get(self, name):
        try:
            return next(g.get(name) for g in self._groups if g.hasany(name))
        except StopIteration:
            return super().get(name)
    
    def getarg(self, name):
        try:
            return next(g.getarg(name) for g in self._groups if name in g.arg_map)
        except StopIteration:
            return super().getarg(name)
    
    def getflag(self, name):
        try:
            return next(g.getflag(name) for g in self._groups if g.hasflag(name))
        except StopIteration:
            return super().getflag(name)
    
    def getcmd(self, name):
        try:
            return next(g.getcmd(name) for g in self._groups if g.hascmd(name))
        except StopIteration:
            return super().getcmd(name)
    
    def hasflag(self, name):
        return super().hasflag(name) or any(g.hasflag(name) for g in self._groups)
    
    def hascmd(self, name):
        return super().hascmd(name) or any(g.hascmd(name) for g in self._groups)
    
    def hasany(self, name):
        return super().hasany(name) or self._aliases.get(name, _Null) in self.arg_map
    
    def enforce_clumps(self, parsed):
        p = set(parsed).union(next((g.name for g in self._groups if g.hasany(i)), None) for i in parsed) - {None}
        return (
          super().enforce_clumps(p, {g.name: g for g in self._groups})
          and
          all(g.enforce_clumps(parsed) for g in self._groups if g.name in p)
          )
    
    def _put_nsp(self, namespaces, entity):
        return entity if entity.namespace is None else partial(
          entity,
          namespaces.setdefault(
            entity.name,
            ErgoNamespace(**entity.namespace)
            )
          )
    
    def _extract_flargs(self, inp, strict=False):
        flags = []
        args = []
        skip = 0
        command = None
        allow_flags = True
        
        too_many_args = False
        unknown_flags = []

        for idx, value in enumerate(inp, 1):
            if skip > 0:
                skip -= 1
                continue
            
            if value == '--':
                allow_flags = False
                continue
            
            if (not allow_flags) or (not value.startswith(self.flag_prefix) or value in (self.flag_prefix, self.long_prefix)):
                if self.hascmd(value):
                    command = (value, idx)
                    break
                args.append(value)
                if strict and len(args) > len(self.args):
                    too_many_args = True
            elif allow_flags:
                if '=' in value:
                    name, arg = value.lstrip(self.flag_prefix).split('=', 1)
                    if self.hasflag(name):
                        flags.append((self.dealias(name), [arg] if arg else []))
                    elif strict:
                        unknown_flags.append(('', value.split('=')[0], idx))
                    continue
                
                if value.startswith(self.long_prefix):
                    if self.hasflag(value.lstrip(self.flag_prefix)):  # long
                        skip = self.getflag(value.lstrip(self.flag_prefix)).argcount
                        next_pos = next((i for i, v in enumerate(inp[idx:]) if v.startswith(self.flag_prefix)), len(inp))
                        if next_pos < skip:
                            skip = next_pos
                        flags.append((self.dealias(value.lstrip(self.flag_prefix)), inp[idx:skip+idx]))
                    elif strict:
                        unknown_flags.append(('', value, idx))
                    continue
                
                for name in value[1:]:  # short
                    if self.hasflag(name):
                        skip = self.getflag(name).argcount
                        next_pos = next((i for i, v in enumerate(inp[idx:]) if v.startswith(self.flag_prefix)), len(inp))
                        if next_pos < skip:
                            skip = next_pos
                        flags.append((self.dealias(name), inp[idx:skip+idx]))
                    elif strict:
                        unknown_flags.append((value[0], name, idx))
        
        if self.default_command is None:
            if too_many_args:
                raise TypeError('Too many positional arguments (expected {}, got {})'.format(
                  len(self.args), len(args)
                ))
            if unknown_flags:
                raise TypeError('Unknown flag(s)' + ' '.join(starmap("`{}{}'".format, unknown_flags)))
        elif command is None:
            unknown_flags = {i[2] for i in unknown_flags}
            inp[:] = [v for i, v in enumerate(inp) if i not in unknown_flags]
        
        return flags, args, command
    
    def do_parse(self, inp=None, strict=False, systemexit=True):
        parsed = {}
        namespaces = {}
        flags, positionals, command = self._extract_flargs(inp, strict)
        prep = partial(self._put_nsp, namespaces)
        
        for flag, args in flags:
            if self.hasflag(flag):
                entity = self.getflag(flag)
                parsed[entity.identifier] = prep(entity)(*args)
        
        if self.default_command is not None and command is None:
            command = self._aliases.get(self.default_command, self.default_command)
            try:
                parsed[command] = self.getcmd(command).do_parse(inp, strict, systemexit)
            except Exception as e:
                if systemexit is None and self.getcmd(command).systemexit or systemexit:
                    self.getcmd(command).error(e, help=False)
                raise
            return ErgoNamespace(**self._defaults, **parsed)._.set_default_key(self.default_command)
        
        if self._last_arg_consumes and len(positionals) > len(self.args):
            zipped_args = zip_longest(map(self.getarg, self.args), positionals, fillvalue=self.getarg(self.args[-1]))
        else:
            zipped_args = zip(map(self.getarg, self.args), positionals)
        
        for entity, value in zipped_args:
            parsed[entity.identifier] = prep(entity)(value)
        
        if command is not None:
            value, idx = command
            try:
                parsed[self._aliases.get(value, value)] = self.getcmd(value).do_parse(inp[idx:], strict, systemexit)
            except Exception as e:
                if systemexit is None and self.getcmd(value).systemexit or systemexit:
                    self.getcmd(value).error(e, help=False)
                raise
        
        self.enforce_clumps(parsed)
        final = {**self._defaults, **{name: value for g in self._groups for name, value in g._defaults.items()}, **parsed}
        nsp = ErgoNamespace(**final)
        
        if self._required.difference(nsp):
            raise errors.RequirementError('Expected the following required arguments: {}\nGot {}'.format(
              ', '.join(map(repr, self._required)),
              ', '.join(map(repr, self._required.intersection(nsp))) or 'none'
              )
            )
        return nsp
    
    def parse(self, inp=None, *, systemexit=None, strict=False, require_main=False, ignore_pkgs=None):
        if require_main and not _private.importer_is_main(depth=require_main, ignore_pkgs=ignore_pkgs):
            return self.defaults
        
        if inp is None:
            inp = sys.argv[1:]
        if isinstance(inp, str):
            inp = shlex.split(inp)
        
        try:
            return self.do_parse(list(inp), strict, systemexit)  # == inp.copy()
        except Exception as e:
            if systemexit is None and self.systemexit or systemexit:
                self.error(e, help=False)
            raise
        except SystemExit:
            if systemexit is None and self.systemexit or systemexit:
                raise


class SubHandler(_Handler):
    def __init__(self, parent, name):
        if not name:
            raise ValueError('Sub-handler name cannot be empty')
        self.name = name
        self.parent = parent
        super().__init__()
    
    @property
    def parent_and(self):
        return ClumpGroup(self._aliases.get(i, i) for i in chain(self._and, self.parent.parent_and))
    
    @property
    def parent_or(self):
        return ClumpGroup(self._aliases.get(i, i) for i in chain(self._or, self.parent.parent_or))
    
    @property
    def parent_xor(self):
        return ClumpGroup(self._aliases.get(i, i) for i in chain(self._xor, self.parent.parent_xor))


class Group(SubHandler):
    def __init__(self, *, required=False, AND=_Null, OR=_Null, XOR=_Null):
        """
        CLI later calls `SubHandler.__init__(Group(), self, name)` in its __setattr__()
        (All instance attributes are overridden by this)
        """
        self._required = required
        self._and = AND
        self._or = OR
        self._xor = XOR
    
    def arg(self, n=1, **kwargs):
        """
        n: number of times this arg should be received consecutively; pass ... for infinite
        Expected kwargs: _ (str), help (str)
        """
        def inner(cb):
            entity = Arg(cb, n, namespace=kwargs.get('namespace'), help=kwargs.get('help'))
            self.arg_map[entity.name] = entity
            if kwargs.get('required'):
                self._required.add(entity.name)
            return self.parent.arg(n, **kwargs)(entity.func)
        return inner
    
    def flag(self, dest=None, short=_Null, **kwargs):
        def inner(cb):
            entity = Flag(cb, namespace=kwargs.get('namespace'), name=dest, help=kwargs.get('help'), _=kwargs.get('_', '-'))
            if dest is not None:
                self._aliases[cb.__name__] = entity.name
            if short is not None:  # _Null == default; None == none
                try:
                    entity.short = short or next(s for s in entity.name if s.isalnum() and s not in self._aliases)
                except StopIteration:
                    pass
                else:
                    self._aliases[entity.short] = entity.name
            if kwargs.get('default', _Null) is not _Null:  # could be `'default' not in ...` but we don't want them using _Null either
                self._defaults[entity.identifier] = kwargs['default']
            if kwargs.get('required'):
                self._required.add(entity.name)
            self.flags[entity.name] = entity
            return self.parent.flag(dest, short, **kwargs)(entity.func)
        return inner


class Command(SubHandler, ParserBase):
    def __init__(self, flag_prefix='-', *, parent, name, desc):
        SubHandler.__init__(self, parent, name)
        ParserBase.__init__(self, desc, flag_prefix, systemexit=getattr(parent, 'systemexit', True))
    
    def __str__(self):
        return ' {}'.format(self.name)
    
    @property
    def help(self):
        return self.format_help()
    
    @classmethod
    def from_cli(cls, cli, parent, name):
        obj = cls(cli.flag_prefix, parent=parent, name=name, desc=cli.desc)
        obj.__dict__.update(vars(cli))
        return obj


class CLI(ParserBase):
    def __str__(self):  # for help screen; main CLI shouldn't show its own name
        return ''
