"""
JIT compiles a tiny subset of Python to x86-64 machine code.
Only relies on stock Python.

Explanation on https://csl.name/post/python-compiler/

Tested on Python 3.4.

Written by Christian Stigen Larsen
Put in the public domain by the author, 2017

Modified by Berker Peksag

TODO: Try to make a register allocator.
TODO: Create more peephole optimizations.
TODO: Add support for calling other functions, loops.
TODO: Try to implement PEP 523.
"""

import ctypes
import dis
import sys

import mj

# Used for compatibility with Python 2.7 and 3+
PRE36 = sys.version_info[:2] < (3, 6)


class Assembler:
    """
    An x86-64 assembler.

    To find the encoding of the instructions, I mainly used the
    NASM assembler. Putting the following in a file sandbox.asm,

        bits 64
        section .text
        mov rax, rcx
        mov rax, rdx
        mov rax, rbx
        mov rax, rsp

    I assembled it with

        $ nasm -felf64 sandbox.asm -osandbox.o

    (-fmacho64 for macOS) and dumped the machine code with

        $ objdump -d sandbox.o

    sandbox.o:     file format elf64-x86-64

    Disassembly of section .text:

    0000000000000000 <.text>:
       0:   48 89 c8                mov    %rcx,%rax
       3:   48 89 d0                mov    %rdx,%rax
       6:   48 89 d8                mov    %rbx,%rax
       9:   48 89 e0                mov    %rsp,%rax

    It seems like the 64-bit movq (which we just call mov) is
    encoded with the prefix 0x48 0x89 with the source and
    destination registers stored in the last byte. Digging
    into a few manuals, we see that they are encoded using
    three bits each.

        0x48, 0x29, 0xc0 | self.registers(b, a)

    If self.registers(b, a) returns 2, the last byte will be

        >>> 0xc0 | 2
        0xc2

    See the registers method for the implementation.
    """

    def __init__(self, size):
        self.block = mj.create_block(size)
        self.index = 0
        self.size = size

    @property
    def raw(self):
        """Returns machine code as a raw string."""
        return bytes(self.block[:self.index])

    @property
    def address(self):
        """Returns address of block in memory."""
        block_address = ctypes.c_uint64.from_buffer(self.block, 0)
        block_p = mj.c_uint8_p(block_address)
        return ctypes.cast(block_p, ctypes.c_void_p).value

    def little_endian(self, n):
        """Converts 64-bit number to little-endian format."""
        return [(n & (0xff << i*2)) >> i*8 for i in range(8)]

    def registers(self, a, b=None):
        """Encodes one or two registers for machine code instructions."""
        order = ("rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi")
        enc = order.index(a)
        if b is not None:
            enc = enc << 3 | order.index(b)
        return enc

    def emit(self, *args):
        """Writes machine code to memory block."""
        for code in args:
            self.block[self.index] = code
            self.index += 1

    def ret(self, a, b):
        self.emit(0xc3)

    def push(self, a, _):
        self.emit(0x50 | self.registers(a))

    def pop(self, a, _):
        self.emit(0x58 | self.registers(a))

    def imul(self, a, b):
        self.emit(0x48, 0x0f, 0xaf, 0xc0 | self.registers(a, b))

    def add(self, a, b):
        self.emit(0x48, 0x01, 0xc0 | self.registers(b, a))

    def sub(self, a, b):
        self.emit(0x48, 0x29, 0xc0 | self.registers(b, a))

    def neg(self, a, _):
        self.emit(0x48, 0xf7, 0xd8 | self.register(a))

    def mov(self, a, b):
        self.emit(0x48, 0x89, 0xc0 | self.registers(b, a))

    def immediate(self, a, number):
        self.emit(0x48, 0xb8 | self.registers(a), *self.little_endian(number))


class Compiler:
    """
    Compiles Python bytecode to intermediate representation (IR).

    CPython is implemented as a stack machine. All the bytecode
    instructions operate on a stack of objects.

    A beautiful property of postfix systems is that operations
    can be serialized:

        2 2 * 3 3 * -

    Moving from left to right, we push 2 on the stack, then
    another 2. For the * operation we pop them both off and push
    their product 4. Push 3 and 3, pop them off and push their
    product 9. The stack will now contain 9 on the top and 4 at
    the bottom. For the final subtraction, we pop them off,
    perform the subtraction and push the result -5 on the stack.

    In postfix form, the evaluation order becomes explicit:

        push 2
        push 2
        call multiply
        push 3
        push 3
        call multiply
        call subtract

    The multiply and subtract functions find their arguments on
    the stack. For subtract, the two arguments consist of the
    products 2*2 and 3*3.

    The use of a stack makes it possible to execute instructions
    linearly, and this is essentially how stack machines operate.

    Our IR will consist of pseudo-assembly instructions in a
    list, with a faint resemblance to three address codes (TAC).
    For example:

        ir = [("mov", "rax", 101),
              ("push", "rax", None)]

    Contrary to TAC, we put the operation first, followed by the
    destination and source registers. We use None to indicate
    unused registers and arguments.

    We will reserve registers RAX and RBX for menial work like
    arithmetic, pushing and popping. RAX must also hold the
    return value, because that's the convention. The CPU already
    has a stack, so we'll use that as our data stack mechanism.

    Registers RDI, RSI, RDX and RCX will be reserved for variables
    and arguments. Per AMD64 convention, we expect to see function
    arguments passed in those registers, in that order.
    """

    def __init__(self, bytecode, constants):
        self.bytecode = bytecode
        self.constants = constants
        self.index = 0

    def fetch(self):
        """Retrieves the next bytecode."""
        byte = self.bytecode[self.index]
        self.index += 1
        return byte

    def decode(self):
        """Fetches the opcode, look up its name and fetch any arguments."""
        opcode = self.fetch()
        opname = dis.opname[opcode]
        if opname.startswith(("UNARY", "BINARY", "INPLACE", "RETURN")):
            argument = None
            if not PRE36:
                self.fetch()
        else:
            argument = self.fetch()
            if PRE36:
                argument |= self.fetch() << 8

        return opname, argument

    def variable(self, number):
        # AMD64 argument passing order for our purposes.
        order = ("rdi", "rsi", "rdx", "rcx")
        return order[number]

    def compile(self):
        while self.index < len(self.bytecode):
            op, arg = self.decode()

            if op == "LOAD_FAST":
                yield "push", self.variable(arg), None

            elif op == "STORE_FAST":
                yield "pop", "rax", None
                yield "mov", self.variable(arg), "rax"

            elif op == "LOAD_CONST":
                yield "immediate", "rax", self.constants[arg]
                yield "push", "rax", None

            elif op == "BINARY_MULTIPLY":
                yield "pop", "rax", None
                yield "pop", "rbx", None
                yield "imul", "rax", "rbx"
                yield "push", "rax", None

            elif op in ("BINARY_ADD", "INPLACE_ADD"):
                yield "pop", "rax", None
                yield "pop", "rbx", None
                yield "add", "rax", "rbx"
                yield "push", "rax", None

            elif op in ("BINARY_SUBTRACT", "INPLACE_SUBTRACT"):
                yield "pop", "rbx", None
                yield "pop", "rax", None
                yield "sub", "rax", "rbx"
                yield "push", "rax", None

            elif op == "UNARY_NEGATIVE":
                yield "pop", "rax", None
                yield "neg", "rax", None
                yield "push", "rax", None

            elif op == "RETURN_VALUE":
                yield "pop", "rax", None
                yield "ret", None, None
            else:
                raise NotImplementedError(op)


def optimize(ir):
    """Performs peephole optimizations on the IR."""
    def fetch(n):
        if n < len(ir):
            return ir[n]
        else:
            return None, None, None

    index = 0
    while index < len(ir):
        op1, a1, b1 = fetch(index)
        op2, a2, b2 = fetch(index + 1)
        op3, a3, b3 = fetch(index + 2)
        op4, a4, b4 = fetch(index + 3)

        # Remove nonsensical moves
        if op1 == "mov" and a1 == b1:
            index += 1
            continue

        # Translate
        #    mov rsi, rax
        #    mov rbx, rsi
        # to mov rbx, rax
        if op1 == op2 == "mov" and a1 == b2:
            index += 2
            yield "mov", a2, b1
            continue

        # Short-circuit push x/pop y
        if op1 == "push" and op2 == "pop":
            index += 2
            yield "mov", a2, a1
            continue

        # Same as above, but with an in-between instruction
        if op1 == "push" and op3 == "pop" and op2 not in ("push", "pop"):
            # Only do this if a3 is not mofidied in the middle instruction. An
            # obvious improvement would be to allow an arbitrary number of
            # in-between instructions.
            if a2 != a3:
                index += 3
                yield "mov", a3, a1
                yield op2, a2, b2
                continue

        # TODO: Generalize this, then remove the previous two
        # Same as above, but with an in-between instruction
        if (op1 == "push" and op4 == "pop" and op2 not in ("push", "pop") and
                op3 not in ("push", "pop")):
            # Only do this if a3 is not modified in the middle instruction. An
            # obvious improvement would be to allow an arbitrary number of
            # in-between instructions.
            if a2 != a4 and a3 != a4:
                index += 4
                yield "mov", a4, a1
                yield op2, a2, b2
                yield op3, a3, b3
                continue

        index += 1
        yield op1, a1, b1


def print_ir(ir):
    for instruction in ir:
        op, args = instruction[0], instruction[1:]
        args = filter(lambda x: x is not None, args)
        print("  %-6s %s" % (op, ", ".join(map(str, args))))


def compile_native(function):
    print("Python disassembly:")
    dis.dis(function)
    print()

    codeobj = function.__code__
    print("Bytecode: %r" % codeobj.co_code)
    print()

    print("Intermediate code:")
    constants = codeobj.co_consts

    python_bytecode = list(codeobj.co_code)

    if sys.version_info.major == 2:
        python_bytecode = map(ord, codeobj.co_code)

    ir = Compiler(python_bytecode, constants).compile()
    ir = list(ir)
    print_ir(ir)
    print()

    print("Optimization:")
    while True:
        optimized = list(optimize(ir))
        reduction = len(ir) - len(optimized)
        ir = optimized
        print("  - removed %d instructions" % reduction)
        if not reduction:
            break
    print_ir(ir)
    print()

    # Compile to native code
    assembler = Assembler(mj.PAGESIZE)
    for name, a, b in ir:
        emit = getattr(assembler, name)
        emit(a, b)

    argcount = codeobj.co_argcount

    if argcount == 0:
        signature = ctypes.CFUNCTYPE(None)
    else:
        # Assume all arguments are 64-bit
        signature = ctypes.CFUNCTYPE(*[ctypes.c_int64] * argcount)

    signature.restype = ctypes.c_int64
    return signature(assembler.address), assembler


def jit(function):
    """Decorator that JIT-compiles function to native code on first call.

    Use this on non-class functions, because our compiler does not support
    objects (rather, it does not support the attr bytecode instructions).

    Example:

        @jit
        def foo(a, b):
            return a*a - b*b
    """
    print("--- Installing JIT for %s" % function)

    def frontend(*args, **kw):
        if not hasattr(frontend, "function"):
            try:
                print("--- JIT-compiling %s" % function)
                native, asm = compile_native(function)
                native.raw = asm.raw
                native.address = asm.address
                frontend.function = native
            except Exception as e:
                frontend.function = function  # fallback to Python
                print("--- Could not compile %s: %s: %s" % (function.__name__,
                      type(e).__name__, e))
        return frontend.function(*args, **kw)
    return frontend


def disassemble(function):
    """Returns disassembly string of natively compiled function.
    Requires the Capstone module."""
    if hasattr(function, "function"):
        function = function.function

    def hexbytes(raw):
        return "".join("%02x " % b for b in raw)

    try:
        import capstone

        out = ""
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)

        for i in md.disasm(function.raw, function.address):
            out += "0x%x %-15s%s %s\n" % (i.address, hexbytes(i.bytes), i.mnemonic, i.op_str)
            if i.mnemonic == "ret":
                break

        return out
    except ImportError:
        print("You need to install the Capstone module for disassembly",
              file=sys.stderr)
