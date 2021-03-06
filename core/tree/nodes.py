from core import tokens as tks
from core import ctypes
import core.tree.decl_nodes as decl_nodes

from core.errors import CompilerError
from core.tree.utils import DirectLValue, report_err, set_type, check_cast

class Node:
    def __init__(self):
        self.r = None

    def make_il(self, il_code, symbol_table, c):
        raise NotImplementedError

class Root(Node):
    def __init__(self, nodes):
        super().__init__()
        self.nodes = nodes

    def make_il(self, il_code, symbol_table, c):
        for node in self.nodes:
            with report_err():
                c = c.set_global(True)
                node.make_il(il_code, symbol_table, c)

class Compound(Node):
    def __init__(self, items):
        super().__init__()
        self.items = items

    def make_il(self, il_code, symbol_table, c, no_scope=False):
        if not no_scope:
            symbol_table.new_scope()

        c = c.set_global(False)
        for item in self.items:
            with report_err():
                item.make_il(il_code, symbol_table, c)

        if not no_scope:
            symbol_table.end_scope()

class EmptyStatement(Node):
    def __init__(self):
        super().__init__()

    def make_il(self, il_code, symbol_table, c):
        pass


class ExprStatement(Node):
    def __init__(self, expr):
        super().__init__()
        self.expr = expr

    def make_il(self, il_code, symbol_table, c):
        self.expr.make_il(il_code, symbol_table, c)

class DeclInfo:
    AUTO = 1
    STATIC = 2
    EXTERN = 3
    TYPEDEF = 4

    def __init__(self, identifier, ctype, range,
                 storage=None, init=None, body=None, param_names=None):
        self.identifier = identifier
        self.ctype = ctype
        self.range = range
        self.storage = storage
        self.init = init
        self.body = body
        self.param_names = param_names

    def process(self, il_code, symbol_table, c):
        if not self.identifier:
            err = "missing identifier name in declaration"
            raise CompilerError(err, self.range)

        # The typedef is special
        if self.storage == self.TYPEDEF:
            self.process_typedef(symbol_table)
            return

        if self.body and not self.ctype.is_function():
            err = "function definition provided for non-function type"
            raise CompilerError(err, self.range)

        linkage = self.get_linkage(symbol_table, c)
        defined = self.get_defined(symbol_table, c)
        storage = self.get_storage(defined, linkage, symbol_table)

        if not c.is_global and self.init and linkage:
            err = "local variable with linkage has initializer"
            raise CompilerError(err, self.range)

        var = symbol_table.add_variable(
            self.identifier,
            self.ctype,
            defined,
            linkage,
            storage)

        if self.init:
            self.do_init(var, storage, il_code, symbol_table, c)
        if self.body:
            self.do_body(il_code, symbol_table, c)

        if not linkage and self.ctype.is_incomplete():
            err = "variable of incomplete type declared"
            raise CompilerError(err, self.range)

    def process_typedef(self, symbol_table):
        if self.init:
            err = "typedef cannot have initializer"
            raise CompilerError(err, self.range)

        if self.body:
            err = "function definition cannot be a typedef"
            raise CompilerError(err, self.range)

        symbol_table.add_typedef(self.identifier, self.ctype)

    def do_init(self, var, storage, il_code, symbol_table, c):
        init = self.init.make_il(il_code, symbol_table, c)
        if storage == symbol_table.STATIC and not init.literal:
            err = ("non-constant initializer for variable with static "
                   "storage duration")
            raise CompilerError(err, self.init.r)
        elif storage == symbol_table.STATIC:
            il_code.static_initialize(var, getattr(init.literal, "val", None))
        elif var.ctype.is_arith() or var.ctype.is_pointer():
            lval = DirectLValue(var)
            lval.set_to(init, il_code, self.identifier.r)
        else:
            err = "declared variable is not of assignable type"
            raise CompilerError(err, self.range)

    def do_body(self, il_code, symbol_table, c):
        is_main = self.identifier.content == "main"

        for param in self.param_names:
            if not param:
                err = "function definition missing parameter name"
                raise CompilerError(err, self.range)

        if is_main:
            self.check_main_type()

        c = c.set_return(self.ctype.ret)
        il_code.start_func(self.identifier.content)

        symbol_table.new_scope()

        num_params = len(self.ctype.args)
        iter = zip(self.ctype.args, self.param_names, range(num_params))
        for ctype, param, i in iter:
            arg = symbol_table.add_variable(
                param, ctype, symbol_table.DEFINED, None,
                symbol_table.AUTOMATIC)
            il_code.add(value_cmds.LoadArg(arg, i))

        self.body.make_il(il_code, symbol_table, c, no_scope=True)
        if not il_code.always_returns() and is_main:
            zero = ILValue(ctypes.integer)
            il_code.register_literal_var(zero, 0)
            il_code.add(control_cmds.Return(zero))
        elif not il_code.always_returns():
            il_code.add(control_cmds.Return(None))

        symbol_table.end_scope()

    def check_main_type(self):
        if not self.ctype.ret.compatible(ctypes.integer):
            err = "'main' function must have integer return type"
            raise CompilerError(err, self.range)
        if len(self.ctype.args) not in {0, 2}:
            err = "'main' function must have 0 or 2 arguments"
            raise CompilerError(err, self.range)
        if self.ctype.args:
            first = self.ctype.args[0]
            second = self.ctype.args[1]

            if not first.compatible(ctypes.integer):
                err = "first parameter of 'main' must be of integer type"
                raise CompilerError(err, self.range)

            is_ptr_array = (second.is_pointer() and
                            (second.arg.is_pointer() or second.arg.is_array()))

            if not is_ptr_array or not second.arg.arg.compatible(ctypes.char):
                err = "second parameter of 'main' must be like char**"
                raise CompilerError(err, self.range)

    def get_linkage(self, symbol_table, c):
        """Get linkage type for given decl_info object.

        See 6.2.2 in the C11 spec for details.
        """
        if c.is_global and self.storage == DeclInfo.STATIC:
            linkage = symbol_table.INTERNAL
        elif self.storage == DeclInfo.EXTERN:
            cur_linkage = symbol_table.lookup_linkage(self.identifier)
            linkage = cur_linkage or symbol_table.EXTERNAL
        elif self.ctype.is_function() and not self.storage:
            linkage = symbol_table.EXTERNAL
        elif c.is_global and not self.storage:
            linkage = symbol_table.EXTERNAL
        else:
            linkage = None

        return linkage

    def get_defined(self, symbol_table, c):
        """Determine whether this is a definition."""
        if (c.is_global and self.storage in {None, self.STATIC}
              and self.ctype.is_object() and not self.init):
            return symbol_table.TENTATIVE
        elif self.storage == self.EXTERN and not (self.init or self.body):
            return symbol_table.UNDEFINED
        elif self.ctype.is_function() and not self.body:
            return symbol_table.UNDEFINED
        else:
            return symbol_table.DEFINED

    def get_storage(self, defined, linkage, symbol_table):
        """Determine the storage duration."""
        if defined == symbol_table.UNDEFINED or not self.ctype.is_object():
            storage = None
        elif linkage or self.storage == self.STATIC:
            storage = symbol_table.STATIC
        else:
            storage = symbol_table.AUTOMATIC

        return storage

class Declaration(Node):
    """Line of a general variable declaration(s).

    node (decl_nodes.Root) - a declaration tree for this line
    body (Compound(Node)) - if this is a function definition, the body of
    the function
    """

    def __init__(self, node, body=None):
        """Initialize node."""
        super().__init__()
        self.node = node
        self.body = body

    def make_il(self, il_code, symbol_table, c):
        """Make code for this declaration."""

        self.set_self_vars(il_code, symbol_table, c)
        decl_infos = self.get_decl_infos(self.node)
        for info in decl_infos:
            with report_err():
                info.process(il_code, symbol_table, c)

    def set_self_vars(self, il_code, symbol_table, c):
        """Set il_code, symbol_table, and context as attributes of self.

        Helper function to prevent us from having to pass these three
        arguments into almost all functions in this class.

        """
        self.il_code = il_code
        self.symbol_table = symbol_table
        self.c = c

    def get_decl_infos(self, node):
        """Given a node, returns a list of decl_info objects for that node."""

        any_dec = bool(node.decls)
        base_type, storage = self.make_specs_ctype(node.specs, any_dec)

        out = []
        for decl, init in zip(node.decls, node.inits):
            with report_err():
                ctype, identifier = self.make_ctype(decl, base_type)

                if ctype.is_function():
                    param_identifiers = self.extract_params(decl)
                else:
                    param_identifiers = []

                out.append(DeclInfo(
                    identifier, ctype, decl.r, storage, init,
                    self.body, param_identifiers))

        return out

    def make_ctype(self, decl, prev_ctype):
        if isinstance(decl, decl_nodes.Pointer):
            new_ctype = PointerCType(prev_ctype, decl.const)
        elif isinstance(decl, decl_nodes.Array):
            new_ctype = self._generate_array_ctype(decl, prev_ctype)
        elif isinstance(decl, decl_nodes.Function):
            new_ctype = self._generate_func_ctype(decl, prev_ctype)
        elif isinstance(decl, decl_nodes.Identifier):
            return prev_ctype, decl.identifier

        return self.make_ctype(decl.child, new_ctype)

    def _generate_array_ctype(self, decl, prev_ctype):
        """Generate a function ctype from a given a decl_node."""

        if decl.n:
            il_value = decl.n.make_il(self.il_code, self.symbol_table, self.c)
            if not il_value.ctype.is_integral():
                err = "array size must have integral type"
                raise CompilerError(err, decl.r)
            if not il_value.literal:
                err = "array size must be compile-time constant"
                raise CompilerError(err, decl.r)
            if il_value.literal.val <= 0:
                err = "array size must be positive"
                raise CompilerError(err, decl.r)
            if not prev_ctype.is_complete():
                err = "array elements must have complete type"
                raise CompilerError(err, decl.r)
            return ArrayCType(prev_ctype, il_value.literal.val)
        else:
            return ArrayCType(prev_ctype, None)

    def _generate_func_ctype(self, decl, prev_ctype):
        """Generate a function ctype from a given a decl_node."""

        # Prohibit storage class specifiers in parameters.
        for param in decl.args:
            decl_info = self.get_decl_infos(param)[0]
            if decl_info.storage:
                err = "storage class specified for function parameter"
                raise CompilerError(err, decl_info.range)

        # Create a new scope because if we create a new struct type inside
        # the function parameters, it should be local to those parameters.
        self.symbol_table.new_scope()
        args = [
            self.get_decl_infos(decl)[0].ctype
            for decl in decl.args
        ]
        self.symbol_table.end_scope()

        # adjust array and function parameters
        has_void = False
        for i in range(len(args)):
            ctype = args[i]
            if ctype.is_array():
                args[i] = PointerCType(ctype.el)
            elif ctype.is_function():
                args[i] = PointerCType(ctype)
            elif ctype.is_void():
                has_void = True
        if has_void and len(args) > 1:
            decl_info = self.get_decl_infos(decl.args[0])[0]
            err = "'void' must be the only parameter"
            raise CompilerError(err, decl_info.range)
        if prev_ctype.is_function():
            err = "function cannot return function type"
            raise CompilerError(err, self.r)
        if prev_ctype.is_array():
            err = "function cannot return array type"
            raise CompilerError(err, self.r)

        if not args and not self.body:
            new_ctype = FunctionCType([], prev_ctype, True)
        elif has_void:
            new_ctype = FunctionCType([], prev_ctype, False)
        else:
            new_ctype = FunctionCType(args, prev_ctype, False)
        return new_ctype

    def extract_params(self, decl):
        identifiers = []
        func_decl = None
        while decl and not isinstance(decl, decl_nodes.Identifier):
            if isinstance(decl, decl_nodes.Function):
                func_decl = decl
            decl = decl.child

        if not func_decl:
            err = "function definition missing parameter list"
            raise CompilerError(err, self.r)

        for param in func_decl.args:
            decl_info = self.get_decl_infos(param)[0]
            identifiers.append(decl_info.identifier)

        return identifiers

    def make_specs_ctype(self, specs, any_dec):
        spec_range = specs[0].r + specs[-1].r
        storage = self.get_storage([spec.kind for spec in specs], spec_range)

        struct_union_specs = {}
        if any(s.kind in struct_union_specs for s in specs):
            node = [s for s in specs if s.kind in struct_union_specs][0]

            redec = not any_dec and storage is None
            base_type = self.parse_struct_union_spec(node, redec)

        # is a typedef
        elif any(s.kind == tks.identifier for s in specs):
            ident = [s for s in specs if s.kind == tks.identifier][0]
            base_type = self.symbol_table.lookup_typedef(ident)

        else:
            base_type = self.get_base_ctype(specs, spec_range)

        if const: base_type = base_type.make_const()
        return base_type, storage

    def get_base_ctype(self, specs, spec_range):
        base_specs = set(ctypes.simple_types)

        our_base_specs = [str(spec.kind) for spec in specs
                          if spec.kind in base_specs]
        specs_str = " ".join(sorted(our_base_specs))

        # replace "long long" with "long" for convenience
        specs_str = specs_str.replace("long long", "long")

        specs = {
            "void": ctypes.void,

            "_Bool": ctypes.bool_t,

            "char": ctypes.char,
            "char signed": ctypes.char,
            "char unsigned": ctypes.unsig_char,

            "short": ctypes.short,
            "short signed": ctypes.short,
            "int short": ctypes.short,
            "int short signed": ctypes.short,
            "short unsigned": ctypes.unsig_short,
            "int short unsigned": ctypes.unsig_short,

            "int": ctypes.integer,
            "signed": ctypes.integer,
            "int signed": ctypes.integer,
            "unsigned": ctypes.unsig_int,
            "int unsigned": ctypes.unsig_int,

            "long": ctypes.longint,
            "long signed": ctypes.longint,
            "int long": ctypes.longint,
            "int long signed": ctypes.longint,
            "long unsigned": ctypes.unsig_longint,
            "int long unsigned": ctypes.unsig_longint,
        }

        if specs_str in specs:
            return specs[specs_str]

        # TODO: provide more helpful feedback on what is wrong
        descrip = "unrecognized set of type specifiers"
        raise CompilerError(descrip, spec_range)

    def get_storage(self, spec_kinds, spec_range):
        """Determine the storage class from given specifier token kinds.

        If no storage class is listed, returns None.
        """
        storage_classes = {}

        storage = None
        for kind in spec_kinds:
            if kind in storage_classes and not storage:
                storage = storage_classes[kind]
            elif kind in storage_classes:
                descrip = "too many storage classes in declaration specifiers"
                raise CompilerError(descrip, spec_range)

        return storage

    
    def _check_struct_member_decl_info(self, decl_info, kind, members):
        """Check whether given decl_info object is a valid struct member."""

        if decl_info.identifier is None:
            # someone snuck an abstract declarator into here!
            err = f"missing name of {kind} member"
            raise CompilerError(err, decl_info.range)

        if decl_info.storage is not None:
            err = f"cannot have storage specifier on {kind} member"
            raise CompilerError(err, decl_info.range)

        if decl_info.ctype.is_function():
            err = f"cannot have function type as {kind} member"
            raise CompilerError(err, decl_info.range)

        # TODO: 6.7.2.1.18 (allow flexible array members)
        if not decl_info.ctype.is_complete():
            err = f"cannot have incomplete type as {kind} member"
            raise CompilerError(err, decl_info.range)

        # TODO: 6.7.2.1.13 (anonymous structs)
        if decl_info.identifier.content in members:
            err = f"duplicate member '{decl_info.identifier.content}'"
            raise CompilerError(err, decl_info.identifier.r)
