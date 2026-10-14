"""Convert the measured Jericho 3.3.1 Linux checkpoint layout to Fernz.

Adapted from Fernz's V3 tests/state_bridge.py (commit e44e7f5), with locally
measured V4/V5/V8 frames, input results, and interpreter headers. Conversion is
based on public API measurements, not interpreter implementation source.
Only line-input boundaries with screen output enabled are supported. The
original export path is retained for round-trip checks. Legacy files supply
the import template themselves; its header is used only when exporting.
"""

# MIT License
#
# Copyright (c) 2026 Adam Fourney
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import struct
import sys
from dataclasses import replace
from hashlib import sha256
from operator import index

from fernz import GameState, Memory
from fernz._instruction import OperandType, decode
from fernz._profiles import profile_for
from fernz._state import FrameState, InputRequest, OutputState, RandomState, StoredInputRequest

STACK_WORDS = 1024
READ_OPCODE = 0xE4
MISSING_OBSERVATION = b"Observation not available"


def integer(value, low, high, name):
    """Validate one native integer against its measured range."""
    value = index(value)
    if not low <= value <= high:
        raise ValueError(f"Invalid {name}: {value}")
    return value


def byte_array(value, length):
    """Read one native byte array without changing its representation."""
    import numpy as np

    if not isinstance(value, np.ndarray) or value.dtype != np.uint8 or value.shape != (length,):
        raise ValueError(f"Expected a uint8 array with {length} bytes")
    return value.tobytes()


class StateBridge:
    """Translate public snapshots without advancing either interpreter.

    The template supplies native interpreter-owned header settings for export.
    Import can use the legacy save itself as the template. No native snapshot
    contains a story hash or host-seed policy; the caller supplies both.
    """

    def __init__(self, story, template, *, seed):
        """Bind a story, native header template, and explicit host-seed policy."""
        if sys.platform != "linux" or sys.byteorder != "little":
            raise ValueError("The measured adapter supports little-endian Linux only")
        if story.header.version not in (3, 4, 5, 8):
            raise ValueError("State interchange supports V3, V4, V5, and V8")
        self.story = story
        self.seed = integer(seed, 0, 2**31 - 1, "host seed")
        ram, *_ = self._parts(template)
        self._native_header = ram[:64]
        self._import_memory(ram)  # Check the template against the supplied story.

    def _parts(self, state):
        if type(state) is not tuple or len(state) != 9:
            raise ValueError("Expected the nine-entry Jericho 3.3.1 state tuple")
        ram, stack, pc, sp, fp, count, opcode, rng, narrative = state
        ram = byte_array(ram, self.story.header.static_memory)
        stack = byte_array(stack, STACK_WORDS * 2)
        pc = integer(pc, 1, self.story.size - 1, "native PC")
        sp = integer(sp, 0, STACK_WORDS, "native stack pointer")
        fp = integer(fp, sp, STACK_WORDS, "native frame pointer")
        count = integer(count, 0, STACK_WORDS // 4, "native frame count")
        if opcode != READ_OPCODE:
            raise ValueError("State interchange requires pending line input")
        if type(rng) is not tuple or len(rng) != 3 or type(narrative) is not bytes:
            raise ValueError("Invalid RNG or narrative in the native snapshot")
        a, period, counter = rng
        a = integer(a, -(2**31), 2**31 - 1, "native RNG state")
        period = integer(period, 0, 999, "native RNG period")
        counter = integer(counter, 0, 999, "native RNG counter")
        # The inactive counter and the high bit of the affine state do not
        # affect the observed sequence. Fernz stores its canonical active state.
        rng = RandomState(self.seed, a & 0x7FFFFFFF, period, counter if period else 0)
        rng._validate()
        return ram, struct.unpack("<1024H", stack), pc, sp, fp, count, rng

    def _import_memory(self, ram):
        memory = Memory(self.story)
        memory._configure_header()
        data = bytearray(ram)
        self._interpreter_header(data, memory.read_bytes(0, 64))
        memory.write_bytes(0, data)
        return memory

    def _interpreter_header(self, data, header):
        # Standard §11: reset only interpreter-owned fields. Story identity and
        # gameplay flags still pass Memory's protected-header validation.
        version = self.story.header.version
        mask = 0x70 if version == 3 else 0x9C if version == 4 else 0x9D
        data[1] = (data[1] & ~mask) | (header[1] & mask)
        if version == 3:
            # This reserved bit differs in the retained Lurking Horror image.
            data[0x11] = (data[0x11] & ~0x10) | (header[0x11] & 0x10)
        else:
            end = 0x22 if version == 4 else 0x28
            data[0x1E:end] = header[0x1E:end]
            if version >= 5:
                data[0x2C:0x2E] = header[0x2C:0x2E]
                # Unsupported graphics, sound, mouse, undo and menu capabilities.
                flags = int.from_bytes(data[0x10:0x12], "big")
                host = int.from_bytes(header[0x10:0x12], "big")
                data[0x10:0x12] = ((flags & ~0x01B8) | (host & 0x01B8)).to_bytes(2, "big")
        data[0x32:0x34] = header[0x32:0x34]

    @staticmethod
    def _read(memory, address):
        if memory.read_byte(address) != READ_OPCODE:
            raise ValueError("The saved read address does not point to line input")
        instruction = decode(memory, address)
        if (
            instruction.opcode.name not in ("sread", "aread")
            or not 2 <= len(instruction.operands) <= 4
        ):
            raise ValueError("Expected a line-input instruction")
        return instruction

    @staticmethod
    def _resolve_read(memory, frame, instruction):
        stack = list(frame.stack)
        values = []
        for operand in instruction.operands:
            if operand.type != OperandType.VARIABLE:
                value = operand.value
            elif operand.value == 0:
                if not stack:
                    raise ValueError("Read operand underflows the native evaluation stack")
                value = stack.pop()
            elif operand.value < 16:
                if operand.value > len(frame.locals):
                    raise ValueError("Read operand refers to a missing local")
                value = frame.locals[operand.value - 1]
            else:
                address = memory.story.header.globals_table + 2 * (operand.value - 16)
                value = memory.read_word(address)
            values.append(value)
        return values, replace(frame, stack=tuple(stack))

    def from_jericho(self, native):
        """Translate a native input-boundary snapshot without running instructions."""
        ram, words, pc, sp, fp, count, rng = self._parts(native)
        memory = self._import_memory(ram)
        frames = []
        for _ in range(count):
            if fp + 4 > STACK_WORDS:
                raise ValueError("Native frame header is outside the stack")
            flags, parent, low, high = words[fp : fp + 4]
            locals_count, arguments = (flags >> 8) & 15, flags & 255
            discard = bool(flags & 0x1000)
            version = self.story.header.version
            if (
                flags & ~0x1FFF
                or arguments > (3 if version == 3 else 7)
                or (discard and version < 5)
                or low > 511
                or high > (255 if version == 3 else 511 if version in (4, 5) else 1023)
            ):
                raise ValueError("Unsupported native frame metadata")
            if sp > fp - locals_count:
                raise ValueError("Native locals overlap the evaluation stack")
            store_address = (high << 9) | low
            frame = FrameState(
                locals=tuple(words[fp - 1 - i] for i in range(locals_count)),
                stack=tuple(reversed(words[sp : fp - locals_count])),
                return_address=store_address if discard else store_address + 1,
                store=None if discard else memory.read_byte(store_address),
                argument_count=arguments,
            )
            frames.append(frame)
            sp, fp = fp + 4, parent + 1
            if not sp <= fp <= STACK_WORDS:
                raise ValueError("Invalid native parent-frame link")
        if fp != STACK_WORDS:
            raise ValueError("Native frame count does not reach the initial context")
        frames.append(FrameState((), tuple(reversed(words[sp:])), None, None))
        frames.reverse()
        # Native PC is immediately after the opcode byte. Fernz has already
        # decoded the operands; perform their stack pops exactly once here.
        instruction = self._read(memory, pc - 1)
        values, frames[-1] = self._resolve_read(memory, frames[-1], instruction)
        if len(values) == 4 and all(values[2:]):
            raise ValueError("Timed input is not supported")
        request = InputRequest(*values[:2], instruction_address=instruction.address)
        if instruction.store is not None:
            request = StoredInputRequest(
                *values[:2], instruction_address=instruction.address, store=instruction.store
            )
        profile = profile_for(self.story)
        game_over, victory = (
            profile.outcomes(native[8].decode("cp1252", errors="replace"))
            if profile
            else (False, False)
        )
        result = GameState(
            story_sha256=sha256(self.story.data[: self.story.size]).hexdigest(),
            ram=memory.read_bytes(0, self.story.header.static_memory),
            pc=instruction.next_address,
            frames=tuple(frames),
            rng=rng,
            input_request=request,
            screen_enabled=True,
            halted=False,
            game_over=game_over,
            victory=victory,
            # The public native tuple does not expose logical display state.
            output=OutputState(),
        )
        result._validate(self.story)
        return result

    def to_jericho(self, state):
        """Translate a Fernz input-boundary checkpoint to the measured native layout."""
        import numpy as np

        state._validate(self.story)
        if state.halted or not state.screen_enabled:
            raise ValueError("State interchange requires pending input with screen output enabled")
        if state.rng.python_state is not None:
            raise ValueError("Python RNG state cannot be transferred to the reference")
        if state.rng.host_seed != self.seed:
            raise ValueError("The destination must use the snapshot's host seed")
        memory = Memory(self.story)
        memory._restore_dynamic(state.ram)
        request = state.input_request
        if isinstance(request, StoredInputRequest) and request.character:
            raise ValueError("State interchange requires pending line input")
        instruction = self._read(memory, request.instruction_address)
        if len(instruction.operands) != 2:
            raise ValueError("Export requires a two-operand line read")
        if instruction.store != getattr(request, "store", None):
            raise ValueError("Saved input result variable does not match the read instruction")
        if instruction.next_address != state.pc:
            raise ValueError("Saved PC does not match the pending read instruction")
        # Restore any operands popped while Fernz entered the pending read.
        # Jericho will evaluate them when the next step starts.
        frame = state.frames[-1]
        stack = list(frame.stack)
        operands = list(zip(instruction.operands, (request.text, request.parse), strict=True))
        for operand, value in reversed(operands):
            if operand.type == OperandType.VARIABLE and operand.value == 0:
                stack.append(value)
        frame = replace(frame, stack=tuple(stack))
        values, after = self._resolve_read(memory, frame, instruction)
        if values != [request.text, request.parse] or after != state.frames[-1]:
            raise ValueError("Pending read operands no longer match the saved continuation")
        frames = (*state.frames[:-1], frame)
        words = [0] * STACK_WORDS
        sp = fp = STACK_WORDS

        def push(value):
            nonlocal sp
            if sp == 0:
                raise ValueError("Snapshot exceeds the measured native stack capacity")
            sp -= 1
            words[sp] = value

        for value in frames[0].stack:
            push(value)
        for frame in frames[1:]:
            store_address = frame.return_address - (frame.store is not None)
            if frame.store is not None and memory.read_byte(store_address) != frame.store:
                raise ValueError("Return store byte changed since the routine was called")
            push(store_address >> 9)
            push(store_address & 511)
            push(fp - 1)
            push(
                (len(frame.locals) << 8)
                | frame.argument_count
                | (0x1000 if frame.store is None else 0)
            )
            fp = sp
            for value in (*frame.locals, *frame.stack):
                push(value)
        ram = bytearray(state.ram)
        self._interpreter_header(ram, self._native_header)
        return (
            np.frombuffer(bytes(ram), dtype=np.uint8).copy(),
            np.frombuffer(struct.pack("<1024H", *words), dtype=np.uint8).copy(),
            instruction.address + 1,
            sp,
            fp,
            len(frames) - 1,
            READ_OPCODE,
            (state.rng.state, state.rng.period, state.rng.position),
            MISSING_OBSERVATION,
        )
