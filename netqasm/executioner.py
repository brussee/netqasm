import operator
import numpy as np
from enum import Enum
from itertools import count
from types import GeneratorType
from dataclasses import dataclass
from collections import defaultdict

from qlink_interface import (
    RequestType,
    ReturnType,
    LinkLayerCreate,
    get_creator_node_id,
)

from netqasm.logging import get_netqasm_logger, _setup_instr_logger_formatter, setup_file_logger
from netqasm.logging import _INSTR_LOGGER_FIELDS, _InstrLogHeaders
from netqasm.subroutine import Command, Register, ArrayEntry, ArraySlice, Address
from netqasm.instructions import Instruction, instruction_to_string
from netqasm.sdk.shared_memory import get_shared_memory, setup_registers, Arrays
from netqasm.network_stack import BaseNetworkStack, OK_FIELDS
from netqasm.parsing import parse_address


@dataclass
class EprCmdData:
    subroutine_id: int
    ent_info_array_address: int
    q_array_address: int
    request: tuple
    tot_pairs: int
    pairs_left: int


def inc_program_counter(method):
    def new_method(self, subroutine_id, operands):
        output = method(self, subroutine_id, operands)
        if isinstance(output, GeneratorType):
            yield from output
        self._program_counters[subroutine_id] += 1
    new_method.__name__ == method.__name__
    return new_method


def log_instr(method):
    def new_method(self, subroutine_id, operands):
        if self._instr_logger is not None:
            sid = _INSTR_LOGGER_FIELDS[_InstrLogHeaders.SID]
            prc = _INSTR_LOGGER_FIELDS[_InstrLogHeaders.PRC]
            sit = _INSTR_LOGGER_FIELDS[_InstrLogHeaders.SIT]
            ins = _INSTR_LOGGER_FIELDS[_InstrLogHeaders.INS]
            instr_name = method.__name__[len('_instr_'):]
            sim_time = self._get_simulated_time()
            extra = {
                sid: subroutine_id,
                prc: self._program_counters[subroutine_id],
                sit: sim_time,
                ins: instr_name,
            }
            ops_str = ' '.join(str(op) for op in operands)
            self._instr_logger.info(f"Doing instruction {instr_name} with operands {ops_str}", extra=extra)
        output = method(self, subroutine_id, operands)
        if isinstance(output, GeneratorType):
            yield from output
    new_method.__name__ == method.__name__
    return new_method


class Executioner:

    def __init__(self, name=None, instr_log_dir=None):
        """Executes a sequence of NetQASM instructions.

        The methods starting with `_instr_xxx` define what a given instruction should do and
        returns the new program counter (+1 unless a branching instruction).
        There are default implementations of these methods, however those involving qubits simply logs (DEBUG) what
        is being executed without actually updating any qubit state. For this reason the measurement instruction
        simply leaves the classical register unchanged.

        The intention is that all these methods should be overriden to define what should actually happen
        but the default implementations can be used testing and debugging.

        Parameters
        ----------
        name : str or None
            Give a name to the executioner for logging purposes.
        """
        if name is None:
            self._name = f"{self.__class__}"
        else:
            self._name = name

        self._instruction_handlers = self._get_instruction_handlers()

        # Registers for different apps
        self._registers = {}

        # Arrays stored in memory for different apps
        self._app_arrays = {}

        # Shared memory with host for different apps
        self._shared_memories = {}

        self._qubit_unit_modules = {}

        # There will be seperate program counters for each subroutine
        self._program_counters = defaultdict(int)

        # Keep track of what subroutines are currently handled
        self._subroutines = {}

        # Keep track of which subroutine in the order
        self._next_subroutine_id = 0

        # Keep track of what physical qubit addresses are in use
        self._used_physical_qubit_addresses = []

        # Keep track of the create epr requests in progress
        self._epr_create_requests = {}

        # Keep track of the recv epr requests in progress
        self._epr_recv_requests = defaultdict(list)

        # Handle responsed for entanglement generation
        self._epr_response_handlers = self._get_epr_response_handlers()

        # Keep track of pending epr responses to handle
        self._pending_epr_responses = []

        # Network stack
        self._network_stack = None

        # Timeout for trying to setup circuits
        self._circuit_setup_timeout = 1

        # Logger for instructions
        self._instr_logger = setup_file_logger(
            cls=self.__class__,
            name=self._name,
            log_dir=instr_log_dir,
            filename=f"{str(self._name).lower()}.log",
            formatter=_setup_instr_logger_formatter())

        self._logger = get_netqasm_logger(f"{self.__class__.__name__}({self._name})")

    def _get_simulated_time(self):
        return 0

    @property
    def network_stack(self):
        return self._network_stack

    @network_stack.setter
    def network_stack(self, network_stack):
        if not isinstance(network_stack, BaseNetworkStack):
            raise TypeError(f"network_stack must be an instance of BaseNetworkStack, not {type(network_stack)}")
        self._network_stack = network_stack

    def init_new_application(self, app_id, max_qubits, circuit_rules=None):
        """Sets up a unit module and a shared memory for a new application"""
        self.allocate_new_qubit_unit_module(app_id=app_id, num_qubits=max_qubits)
        self.setup_registers(app_id=app_id)
        self.setup_arrays(app_id=app_id)
        self.new_shared_memory(app_id=app_id)

    def setup_registers(self, app_id):
        """Setup registers for application"""
        self._registers[app_id] = setup_registers()

    def setup_arrays(self, app_id):
        """Setup memory for storing arrays for application"""
        self._app_arrays[app_id] = Arrays()

    def new_shared_memory(self, app_id):
        """Instanciated a new shared memory with an application"""
        self._shared_memories[app_id] = get_shared_memory(node_name=self._name, key=app_id)

    def setup_epr_socket(self, epr_socket_id, remote_node_id, remote_epr_socket_id):
        if self.network_stack is None:
            return
        output = self.network_stack.setup_epr_socket(
            epr_socket_id=epr_socket_id,
            remote_node_id=remote_node_id,
            remote_epr_socket_id=remote_epr_socket_id,
        )
        if isinstance(output, GeneratorType):
            yield from output

    def stop_application(self, app_id):
        """Stops an application and clears all qubits and classical memories"""
        self._clear_qubits(app_id=app_id)
        self._clear_registers(app_id=app_id)
        self._clear_arrays(app_id=app_id)
        self._clear_shared_memory(app_id=app_id)

    def _clear_qubits(self, app_id):
        unit_module = self._qubit_unit_modules.pop(app_id)
        for virtual_address, physical_address in enumerate(unit_module):
            if physical_address is None:
                continue
            self._used_physical_qubit_addresses.remove(physical_address)
            self._clear_phys_qubit_in_memory(physical_address)

    def _clear_registers(self, app_id):
        self._registers.pop(app_id)

    def _clear_arrays(self, app_id):
        self._app_arrays.pop(app_id)

    def _clear_shared_memory(self, app_id):
        self._shared_memories.pop(app_id)

    def reset_program_counter(self, subroutine_id):
        """Resets the program counter for a given subroutine ID"""
        self._program_counters.pop(subroutine_id, 0)

    def clear_subroutine(self, subroutine_id):
        """Clears a subroutine from the executioner"""
        self.reset_program_counter(subroutine_id=subroutine_id)
        self._subroutines.pop(subroutine_id, 0)

    def _get_instruction_handlers(self):
        """Creates the dictionary of instruction handlers"""
        instruction_handlers = {
            instr: getattr(self, f"_instr_{instruction_to_string(instr)}") for instr in Instruction
        }
        return instruction_handlers

    def _get_epr_response_handlers(self):
        epr_response_handlers = {
            ReturnType.ERR: self._handle_epr_err_response,
            ReturnType.OK_K: self._handle_epr_ok_k_response,
            ReturnType.OK_M: self._handle_epr_ok_m_response,
            ReturnType.OK_R: self._handle_epr_ok_r_response,
        }

        return epr_response_handlers

    def _consume_execute_subroutine(self, subroutine):
        """Consumes the generator returned by execute_subroutine"""
        list(self.execute_subroutine(subroutine=subroutine))

    def execute_subroutine(self, subroutine):
        """Executes the a subroutine given to the executioner"""
        subroutine_id = self._get_new_subroutine_id()
        self._subroutines[subroutine_id] = subroutine
        self.reset_program_counter(subroutine_id)
        output = self._execute_commands(subroutine_id, subroutine.commands)
        if isinstance(output, GeneratorType):
            yield from output
        self.clear_subroutine(subroutine_id=subroutine_id)

    def _get_new_subroutine_id(self):
        self._next_subroutine_id += 1
        return self._next_subroutine_id - 1

    def _execute_commands(self, subroutine_id, commands):
        """Executes a given subroutine"""
        while self._program_counters[subroutine_id] < len(commands):
            prog_counter = self._program_counters[subroutine_id]
            command = commands[prog_counter]
            try:
                output = self._execute_command(subroutine_id, command)
                if isinstance(output, GeneratorType):  # sanity check: should always be the case
                    yield from output
            except Exception as exc:
                raise exc.__class__(f"At line {prog_counter}: {exc}") from exc

    def _execute_command(self, subroutine_id, command):
        """Executes a single instruction"""
        if not isinstance(command, Command):
            raise TypeError(f"Expected a Command, not {type(command)}")
        self._assert_number_args(command.args, num=0)
        output = self._instruction_handlers[command.instruction](subroutine_id, command.operands)
        if isinstance(output, GeneratorType):
            yield from output

    @inc_program_counter
    def _instr_set(self, subroutine_id, operands):
        register = operands[0]
        constant = operands[1]
        self._logger.debug(f"Set register {register} to {constant}")
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        self._set_register(app_id, register, constant)

    def _set_register(self, app_id, register, value):
        self._registers[app_id][register.name][register.index] = value

    def _get_register(self, app_id, register):
        return self._registers[app_id][register.name][register.index]

    @inc_program_counter
    def _instr_qalloc(self, subroutine_id, operands):
        register = operands[0]
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        qubit_address = self._get_register(app_id, register)
        self._logger.debug(f"Taking qubit at address {qubit_address}")
        self._allocate_physical_qubit(subroutine_id, qubit_address)

    @inc_program_counter
    @log_instr
    def _instr_init(self, subroutine_id, operands):
        yield from self._handle_single_qubit_instr(Instruction.INIT, subroutine_id, operands)

    @inc_program_counter
    def _instr_store(self, subroutine_id, operands):
        register = operands[0]
        array_entry = operands[1]
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        value = self._get_register(app_id, register)
        self._logger.debug(f"Storing value {value} from register {register} to array entry {array_entry}")
        self._set_array_entry(app_id=app_id, array_entry=array_entry, value=value)

    @inc_program_counter
    def _instr_load(self, subroutine_id, operands):
        register = operands[0]
        array_entry = operands[1]
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        value = self._get_array_entry(app_id=app_id, array_entry=array_entry)
        self._logger.debug(f"Storing value {value} from array entry {array_entry} to register {register}")
        self._set_register(app_id, register, value)

    @inc_program_counter
    def _instr_lea(self, subroutine_id, operands):
        register = operands[0]
        address = operands[1]
        self._logger.debug(f"Storing address of {address} to register {register}")
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        self._set_register(app_id=app_id, register=register, value=address.address)

    @inc_program_counter
    def _instr_undef(self, subroutine_id, operands):
        array_entry = operands[0]
        self._logger.debug(f"Unset array entry {array_entry}")
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        self._set_array_entry(app_id=app_id, array_entry=array_entry, value=None)

    @inc_program_counter
    def _instr_array(self, subroutine_id, operands):
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        length = self._get_register(app_id, operands[0])
        address = operands[1]
        self._logger.debug(f"Initializing an array of length {length} at address {address}")
        self._initialize_array(app_id=app_id, address=address, length=length)

    def _initialize_array(self, app_id, address, length):
        arrays = self._app_arrays[app_id]
        arrays.init_new_array(address.address, length)

    def _instr_jmp(self, subroutine_id, operands):
        self._handle_branch_instr(
            instr=Instruction.JMP,
            subroutine_id=subroutine_id,
            operands=operands,
        )

    def _instr_bez(self, subroutine_id, operands):
        self._handle_branch_instr(
            instr=Instruction.BEZ,
            subroutine_id=subroutine_id,
            operands=operands,
        )

    def _instr_bnz(self, subroutine_id, operands):
        self._handle_branch_instr(
            instr=Instruction.BNZ,
            subroutine_id=subroutine_id,
            operands=operands,
        )

    def _instr_beq(self, subroutine_id, operands):
        self._handle_branch_instr(
            instr=Instruction.BEQ,
            subroutine_id=subroutine_id,
            operands=operands,
        )

    def _instr_bne(self, subroutine_id, operands):
        self._handle_branch_instr(
            instr=Instruction.BNE,
            subroutine_id=subroutine_id,
            operands=operands,
        )

    def _instr_blt(self, subroutine_id, operands):
        self._handle_branch_instr(
            instr=Instruction.BLT,
            subroutine_id=subroutine_id,
            operands=operands,
        )

    def _instr_bge(self, subroutine_id, operands):
        self._handle_branch_instr(
            instr=Instruction.BGE,
            subroutine_id=subroutine_id,
            operands=operands,
        )

    def _handle_branch_instr(self, instr, subroutine_id, operands):
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        a, b = None, None
        if instr != Instruction.JMP:
            a = self._get_register(app_id=app_id, register=operands[0])
        if instr in [Instruction.BEQ, Instruction.BNE, Instruction.BLT, Instruction.BGE]:
            b = self._get_register(app_id=app_id, register=operands[1])

        condition_func = {
            Instruction.JMP: lambda a, b: True,
            Instruction.BEZ: lambda a, b: operator.eq(a, 0),
            Instruction.BNZ: lambda a, b: operator.ne(a, 0),
            Instruction.BEQ: operator.eq,
            Instruction.BNE: operator.ne,
            Instruction.BLT: operator.lt,
            Instruction.BGE: operator.ge,
        }[instr]

        if condition_func(a, b):
            jump_address = operands[-1]
            self._logger.debug(f"Branching to line {jump_address}, since {instr}(a={a}, b={b}) "
                               f"is True, with values from registers {operands[:-1]}")
            self._program_counters[subroutine_id] = jump_address
        else:
            self._logger.debug(f"Don't branch, since {instr}(a={a}, b={b}) "
                               f"is False, with values from registers {operands[:-1]}")
            self._program_counters[subroutine_id] += 1

    @inc_program_counter
    def _instr_add(self, subroutine_id, operands):
        self._handle_binary_classical_instr(Instruction.ADD, subroutine_id, operands)

    @inc_program_counter
    def _instr_addm(self, subroutine_id, operands):
        self._handle_binary_classical_instr(Instruction.ADDM, subroutine_id, operands)

    @inc_program_counter
    def _instr_sub(self, subroutine_id, operands):
        self._handle_binary_classical_instr(Instruction.SUB, subroutine_id, operands)

    @inc_program_counter
    def _instr_subm(self, subroutine_id, operands):
        self._handle_binary_classical_instr(Instruction.SUBM, subroutine_id, operands)

    def _handle_binary_classical_instr(self, instr, subroutine_id, operands):
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        if instr in [Instruction.ADDM, Instruction.SUBM]:
            mod = self._get_register(app_id=app_id, register=operands[3])
        else:
            mod = None
        if mod is not None and mod < 1:
            raise RuntimeError(f"Modulus needs to be greater or equal to 1, not {mod}")
        a = self._get_register(app_id=app_id, register=operands[1])
        b = self._get_register(app_id=app_id, register=operands[2])
        value = self._compute_binary_classical_instr(instr, a, b, mod=mod)
        mod_str = "" if mod is None else f"(mod {mod})"
        self._logger.debug(f"Performing {instr} of a={a} and b={b} {mod_str} "
                           f"and storing the value {value} at register {operands[0]}")
        self._set_register(app_id=app_id, register=operands[0], value=value)

    def _compute_binary_classical_instr(self, instr, a, b, mod=1):
        op = {
            Instruction.ADD: operator.add,
            Instruction.ADDM: operator.add,
            Instruction.SUB: operator.sub,
            Instruction.SUBM: operator.sub,
        }[instr]
        if mod is None:
            return op(a, b)
        else:
            return op(a, b) % mod

    @inc_program_counter
    @log_instr
    def _instr_x(self, subroutine_id, operands):
        yield from self._handle_single_qubit_instr(Instruction.X, subroutine_id, operands)

    @inc_program_counter
    @log_instr
    def _instr_y(self, subroutine_id, operands):
        yield from self._handle_single_qubit_instr(Instruction.Y, subroutine_id, operands)

    @inc_program_counter
    @log_instr
    def _instr_z(self, subroutine_id, operands):
        yield from self._handle_single_qubit_instr(Instruction.Z, subroutine_id, operands)

    @inc_program_counter
    @log_instr
    def _instr_h(self, subroutine_id, operands):
        yield from self._handle_single_qubit_instr(Instruction.H, subroutine_id, operands)

    @inc_program_counter
    @log_instr
    def _instr_s(self, subroutine_id, operands):
        yield from self._handle_single_qubit_instr(Instruction.S, subroutine_id, operands)

    @inc_program_counter
    @log_instr
    def _instr_k(self, subroutine_id, operands):
        yield from self._handle_single_qubit_instr(Instruction.K, subroutine_id, operands)

    @inc_program_counter
    @log_instr
    def _instr_t(self, subroutine_id, operands):
        yield from self._handle_single_qubit_instr(Instruction.T, subroutine_id, operands)

    @inc_program_counter
    @log_instr
    def _instr_rot_x(self, subroutine_id, operands):
        yield from self._handle_single_qubit_rotation(Instruction.ROT_X, subroutine_id, operands)

    @inc_program_counter
    @log_instr
    def _instr_rot_y(self, subroutine_id, operands):
        yield from self._handle_single_qubit_rotation(Instruction.ROT_Y, subroutine_id, operands)

    @inc_program_counter
    @log_instr
    def _instr_rot_z(self, subroutine_id, operands):
        yield from self._handle_single_qubit_rotation(Instruction.ROT_Z, subroutine_id, operands)

    @inc_program_counter
    @log_instr
    def _instr_cnot(self, subroutine_id, operands):
        yield from self._handle_two_qubit_instr(Instruction.CNOT, subroutine_id, operands)

    @inc_program_counter
    @log_instr
    def _instr_cphase(self, subroutine_id, operands):
        yield from self._handle_two_qubit_instr(Instruction.CPHASE, subroutine_id, operands)

    def _handle_single_qubit_instr(self, instr, subroutine_id, operands):
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        q_address = self._get_register(app_id=app_id, register=operands[0])
        self._logger.debug(f"Performing {instr} on the qubit at address {q_address}")
        output = self._do_single_qubit_instr(instr, subroutine_id, q_address)
        if isinstance(output, GeneratorType):
            yield from output

    def _do_single_qubit_instr(self, instr, subroutine_id, address):
        """Performs a single qubit gate"""
        pass

    def _handle_single_qubit_rotation(self, instr, subroutine_id, operands):
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        q_address = self._get_register(app_id=app_id, register=operands[0])
        angle = self._get_rotation_angle_from_operands(app_id=app_id, operands=operands)
        self._logger.debug(f"Performing {instr} with angle {angle} "
                           f"on the qubit at address {q_address}")
        output = self._do_single_qubit_rotation(instr, subroutine_id, q_address, angle=angle)
        if isinstance(output, GeneratorType):
            yield from output

    def _get_rotation_angle_from_operands(self, app_id, operands):
        n = operands[1]
        d = operands[2]
        return n * np.pi / 2 ** d

    def _do_single_qubit_rotation(self, instr, subroutine_id, address, angle):
        """Performs a single qubit rotation with the angle `n * pi / m`"""
        pass

    def _handle_two_qubit_instr(self, instr, subroutine_id, operands):
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        q_address1 = self._get_register(app_id=app_id, register=operands[0])
        q_address2 = self._get_register(app_id=app_id, register=operands[1])
        self._logger.debug(f"Performing {instr} on the qubits at addresses {q_address1} and {q_address2}")
        output = self._do_two_qubit_instr(instr, subroutine_id, q_address1, q_address2)
        if isinstance(output, GeneratorType):
            yield from output

    def _do_two_qubit_instr(self, instr, subroutine_id, address1, address2):
        """Performs a two qubit gate"""
        pass

    @inc_program_counter
    @log_instr
    def _instr_meas(self, subroutine_id, operands):
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        q_address = self._get_register(app_id=app_id, register=operands[0])
        self._logger.debug(f"Measuring the qubit at address {q_address}, "
                           f"placing the outcome in register {operands[1]}")
        outcome = self._do_meas(subroutine_id=subroutine_id, q_address=q_address)
        self._set_register(app_id=app_id, register=operands[1], value=outcome)

    def _do_meas(self, subroutine_id, q_address):
        """Performs a measurement on a single qubit"""
        # Always give outcome zero in the default debug class
        return 0

    @inc_program_counter
    @log_instr
    def _instr_create_epr(self, subroutine_id, operands):
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        remote_node_id = self._get_register(app_id=app_id, register=operands[0])
        epr_socket_id = self._get_register(app_id=app_id, register=operands[1])
        q_array_address = self._get_register(app_id=app_id, register=operands[2])
        arg_array_address = self._get_register(app_id=app_id, register=operands[3])
        ent_info_array_address = self._get_register(app_id=app_id, register=operands[4])
        self._logger.debug(f"Creating EPR pair with remote node id {remote_node_id} and EPR socket ID {epr_socket_id}, "
                           f"using qubit addresses stored in array with address {q_array_address}, "
                           f"using arguments stored in array with address {arg_array_address}, "
                           f"placing the entanglement information in array at address {ent_info_array_address}")
        self._do_create_epr(
            subroutine_id=subroutine_id,
            remote_node_id=remote_node_id,
            epr_socket_id=epr_socket_id,
            q_array_address=q_array_address,
            arg_array_address=arg_array_address,
            ent_info_array_address=ent_info_array_address,
        )

    def _do_create_epr(
        self,
        subroutine_id,
        remote_node_id,
        epr_socket_id,
        q_array_address,
        arg_array_address,
        ent_info_array_address,
    ):
        if self.network_stack is None:
            raise RuntimeError(f"SubroutineHandler has no network stack")
        create_request = self._get_create_request(
            subroutine_id=subroutine_id,
            remote_node_id=remote_node_id,
            epr_socket_id=epr_socket_id,
            arg_array_address=arg_array_address,
        )
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        if create_request.type == RequestType.K:
            num_qubits = len(self._app_arrays[app_id][q_array_address, :])
            assert num_qubits == create_request.number, "Not enough qubit addresses"
        create_id = self.network_stack.put(request=create_request)
        self._epr_create_requests[create_id] = EprCmdData(
            subroutine_id=subroutine_id,
            ent_info_array_address=ent_info_array_address,
            q_array_address=q_array_address,
            request=create_request,
            tot_pairs=create_request.number,
            pairs_left=create_request.number,
        )

    def _get_create_request(self, subroutine_id, remote_node_id, epr_socket_id, arg_array_address):
        purpose_id = self._get_purpose_id(
            remote_node_id=remote_node_id,
            epr_socket_id=epr_socket_id,
        )
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        args = self._app_arrays[app_id][arg_array_address, :]
        args = [remote_node_id, purpose_id] + args

        # Use defaults if not specified
        expected_num_args = len(LinkLayerCreate._fields)
        if len(args) != expected_num_args:
            raise ValueError(f"Expected {expected_num_args} arguments, but got {len(args)}")
        kwargs = {}
        for arg, field, default in zip(args, LinkLayerCreate._fields, LinkLayerCreate.__new__.__defaults__):
            if arg is None:
                kwargs[field] = default
            else:
                kwargs[field] = arg
        kwargs["type"] = RequestType(kwargs["type"])

        return LinkLayerCreate(**kwargs)

    def _get_purpose_id(self, remote_node_id, epr_socket_id):
        # Should be subclassed
        # Note this is for now since we communicate directly to link layer
        if self._network_stack is None:
            raise RuntimeError("Exectioner has not network stack")
        return self._network_stack._get_purpose_id(
            remote_node_id=remote_node_id,
            epr_socket_id=epr_socket_id,
        )

    @inc_program_counter
    @log_instr
    def _instr_recv_epr(self, subroutine_id, operands):
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        remote_node_id = self._get_register(app_id=app_id, register=operands[0])
        epr_socket_id = self._get_register(app_id=app_id, register=operands[1])
        q_array_address = self._get_register(app_id=app_id, register=operands[2])
        ent_info_array_address = self._get_register(app_id=app_id, register=operands[3])
        self._logger.debug(f"Receiving EPR pair with remote node id {remote_node_id} "
                           f"and EPR socket ID {epr_socket_id}, "
                           f"using qubit addresses stored in array with address {q_array_address}, "
                           f"placing the entanglement information in array at address {ent_info_array_address}")
        self._do_recv_epr(
            subroutine_id=subroutine_id,
            remote_node_id=remote_node_id,
            epr_socket_id=epr_socket_id,
            q_array_address=q_array_address,
            ent_info_array_address=ent_info_array_address,
        )

    def _do_recv_epr(self, subroutine_id, remote_node_id, epr_socket_id, q_array_address, ent_info_array_address):
        if self.network_stack is None:
            raise RuntimeError("SubroutineHandler has no network stack")
        # Check number of qubit addresses
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        # Get number of pairs based on length of ent info array
        num_pairs = self._get_num_pairs_from_array(
            app_id=app_id,
            ent_info_array_address=ent_info_array_address,
        )
        purpose_id = self._network_stack._get_purpose_id(
            remote_node_id=remote_node_id,
            epr_socket_id=epr_socket_id,
        )
        self._epr_recv_requests[purpose_id].append(EprCmdData(
            subroutine_id=subroutine_id,
            ent_info_array_address=ent_info_array_address,
            q_array_address=q_array_address,
            request=None,
            tot_pairs=num_pairs,
            pairs_left=num_pairs,
        ))

    def _get_num_pairs_from_array(self, app_id, ent_info_array_address):
        return int(len(self._app_arrays[app_id][ent_info_array_address, :]) / OK_FIELDS)

    @inc_program_counter
    def _instr_wait_all(self, subroutine_id, operands):
        array_slice = operands[0]
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        self._logger.debug(f"Waiting for all entries in array slice {array_slice} to become defined")
        while True:
            values = self._get_array_slice(app_id=app_id, array_slice=array_slice)
            if any(value is None for value in values):
                output = self._do_wait()
                if isinstance(output, GeneratorType):
                    yield from output
            else:
                break
        self._logger.debug(f"Finished waiting")

    @inc_program_counter
    def _instr_wait_any(self, subroutine_id, operands):
        array_slice = operands[0]
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        self._logger.debug(f"Waiting for any entry in array slice {array_slice} to become defined")
        while True:
            values = self._get_array_slice(app_id=app_id, array_slice=array_slice)
            if all(value is None for value in values):
                output = self._do_wait()
                if isinstance(output, GeneratorType):
                    yield from output
            else:
                break
        self._logger.debug(f"Finished waiting")

    @inc_program_counter
    def _instr_wait_single(self, subroutine_id, operands):
        array_entry = operands[0]
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        self._logger.debug(f"Waiting for array entry {array_entry} to become defined")
        while True:
            value = self._get_array_entry(app_id=app_id, array_entry=array_entry)
            if value is None:
                output = self._do_wait()
                if isinstance(output, GeneratorType):
                    yield from output
            else:
                break
        self._logger.debug(f"Finished waiting")

    def _do_wait(self):
        pass

    @inc_program_counter
    def _instr_qfree(self, subroutine_id, operands):
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        q_address = self._get_register(app_id=app_id, register=operands[0])
        self._logger.debug(f"Freeing qubit at virtual address {q_address}")
        self._free_physical_qubit(subroutine_id, q_address)

    @inc_program_counter
    def _instr_ret_reg(self, subroutine_id, operands):
        register = operands[0]
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        value = self._get_register(app_id=app_id, register=register)
        self._update_shared_memory(app_id=app_id, entry=register, value=value)

    @inc_program_counter
    def _instr_ret_arr(self, subroutine_id, operands):
        address = operands[0]
        app_id = self._get_app_id(subroutine_id=subroutine_id)

        array = self._get_array(app_id=app_id, address=address)
        self._update_shared_memory(app_id=app_id, entry=address, value=array)

    def _update_shared_memory(self, app_id, entry, value):
        shared_memory = self._shared_memories[app_id]
        if isinstance(entry, Register):
            shared_memory.set_register(entry, value)
        elif isinstance(entry, ArrayEntry) or isinstance(entry, ArraySlice):
            address, index = self._expand_array_part(app_id=app_id, array_part=entry)
            shared_memory.set_array_part(address=address, index=index, value=value)
        elif isinstance(entry, Address):
            address = entry.address
            shared_memory.init_new_array(address=address, new_array=value)
        else:
            raise TypeError(f"Cannot update shared memory with entry specified as {entry}")

    def _get_unit_module(self, subroutine_id):
        app_id = self._get_app_id(subroutine_id)
        unit_module = self._qubit_unit_modules.get(app_id)
        if unit_module is None:
            raise RuntimeError(f"Application with app ID {app_id} has not allocated qubit unit module")
        return unit_module

    def _get_position_in_unit_module(self, app_id, address):
        unit_module = self._qubit_unit_modules[app_id]
        if address >= len(unit_module):
            raise IndexError(f"The address {address} is not within the allocated unit module "
                             f"of size {len(unit_module)}")
        position = unit_module[address]
        if position is None:
            raise RuntimeError(f"The qubit with address {address} was not allocated for app ID {app_id}")
        return position

    def _get_array(self, app_id, address):
        return self._app_arrays[app_id]._get_array(address.address)

    def _get_array_entry(self, app_id, array_entry):
        address, index = self._expand_array_part(app_id=app_id, array_part=array_entry)
        return self._app_arrays[app_id][address, index]

    def _set_array_entry(self, app_id, array_entry, value):
        address, index = self._expand_array_part(app_id=app_id, array_part=array_entry)
        self._app_arrays[app_id][address, index] = value

    def _get_array_slice(self, app_id, array_slice):
        address, index = self._expand_array_part(app_id=app_id, array_part=array_slice)
        return self._app_arrays[app_id][address, index]

    def _expand_array_part(self, app_id, array_part):
        address = array_part.address.address
        if isinstance(array_part, ArrayEntry):
            if isinstance(array_part.index, int):
                index = array_part.index
            else:
                index = self._get_register(app_id=app_id, register=array_part.index)
        elif isinstance(array_part, ArraySlice):
            startstop = []
            for raw_s in [array_part.start, array_part.stop]:
                if isinstance(raw_s, int):
                    s = raw_s
                else:
                    s = self._get_register(app_id=app_id, register=raw_s)
                startstop.append(s)
            index = slice(*startstop)
        return address, index

    def allocate_new_qubit_unit_module(self, app_id, num_qubits):
        unit_module = self._get_new_qubit_unit_module(num_qubits)
        self._qubit_unit_modules[app_id] = unit_module

    def _get_new_qubit_unit_module(self, num_qubits):
        return [None] * num_qubits

    def _has_virtual_address(self, app_id, virtual_address):
        unit_module = self._qubit_unit_modules.get(app_id)
        if unit_module is None:
            return False
        return unit_module[virtual_address] is not None

    def _allocate_physical_qubit(self, subroutine_id, virtual_address, physical_address=None):
        unit_module = self._get_unit_module(subroutine_id)
        if virtual_address >= len(unit_module):
            app_id = self._subroutines[subroutine_id].app_id
            raise ValueError(f"Virtual address {virtual_address} is outside the unit module (app ID {app_id}) "
                             f"which has length {len(unit_module)}")
        if unit_module[virtual_address] is None:
            if physical_address is None:
                physical_address = self._get_unused_physical_qubit()
            self._used_physical_qubit_addresses.append(physical_address)
            unit_module[virtual_address] = physical_address
            self._reserve_physical_qubit(physical_address)
        else:
            app_id = self._subroutines[subroutine_id].app_id
            raise RuntimeError(f"QubitAddress at address {virtual_address} "
                               f"for application {app_id} is already allocated")

    def _free_physical_qubit(self, subroutine_id, address):
        unit_module = self._get_unit_module(subroutine_id)
        if unit_module[address] is None:
            app_id = self._subroutines[subroutine_id].app_id
            raise RuntimeError(f"QubitAddress at address {address} for application {app_id} is not allocated "
                               "and cannot be freed")
        else:
            physical_address = unit_module[address]
            unit_module[address] = None
            self._used_physical_qubit_addresses.remove(physical_address)
            self._clear_phys_qubit_in_memory(physical_address)

    def _reserve_physical_qubit(self, physical_address):
        """To be subclassed for different quantum processors (e.g. netsquid)"""
        pass

    def _clear_phys_qubit_in_memory(self, physical_address):
        """To be subclassed for different quantum processors (e.g. netsquid)"""
        pass

    def _get_unused_physical_qubit(self):
        # Assuming that the topology of the unit module is a complete graph
        # is does not matter which unused physical qubit we choose for now
        for physical_address in count(0):
            if physical_address not in self._used_physical_qubit_addresses:
                return physical_address

    def _assert_number_args(self, args, num):
        if not len(args) == num:
            raise TypeError(f"Expected {num} arguments, got {len(args)}")

    def _get_app_id(self, subroutine_id):
        """Returns the app ID for the given subroutine"""
        subroutine = self._subroutines.get(subroutine_id)
        if subroutine is None:
            raise ValueError(f"Unknown subroutine with ID {subroutine_id}")
        return subroutine.app_id

    def _handle_epr_response(self, response):
        self._pending_epr_responses.append(response)
        self._handle_pending_epr_responses()

    def _handle_pending_epr_responses(self):
        # NOTE this will probably be handled differently in an actual implementation
        # but is done in a simple way for now to allow for simulation
        if len(self._pending_epr_responses) == 0:
            return

        response = self._pending_epr_responses[0]

        if response.type == ReturnType.ERR:
            self._handle_epr_err_response(response)
        else:
            self._logger.debug("Handling EPR OK ({response.type}) response from network stack")
            info = self._extract_epr_info(response=response)
            if info is not None:
                epr_cmd_data, pair_index, is_creator, request_key = info
                handled = self._epr_response_handlers[response.type](
                    epr_cmd_data=epr_cmd_data,
                    response=response,
                    pair_index=pair_index,
                )
            else:
                handled = False
            if handled:
                epr_cmd_data.pairs_left -= 1

                self._handle_last_epr_pair(
                    epr_cmd_data=epr_cmd_data,
                    is_creator=is_creator,
                    request_key=request_key,
                )

                self._store_ent_info(
                    epr_cmd_data=epr_cmd_data,
                    response=response,
                    pair_index=pair_index,
                )
                self._pending_epr_responses.pop(0)
            else:
                self._wait_to_handle_epr_responses()
                return

        self._handle_pending_epr_responses()

    def _wait_to_handle_epr_responses(self):
        # This can be subclassed to sleep a little before handling again
        self._handle_pending_epr_responses()

    def _handle_epr_err_response(self, response):
        raise RuntimeError(f"Got the following error from the network stack: {response}")

    def _extract_epr_info(self, response):
        creator_node_id = get_creator_node_id(self._node.ID, response)

        # Retreive the data for this request (depending on if we are creator or receiver
        if creator_node_id == self._node.ID:
            is_creator = True
            create_id = response.create_id
            epr_cmd_data = self._epr_create_requests[create_id]
            request_key = create_id
        else:
            is_creator = False
            purpose_id = response.purpose_id
            if len(self._epr_recv_requests[purpose_id]) == 0:
                self._logger.debug(f"Since there is yet not recv request for purpose ID {purpose_id}, "
                                   "handling of epr will wait and try again.")
                return None
            epr_cmd_data = self._epr_recv_requests[purpose_id][0]
            request_key = purpose_id

        pair_index = epr_cmd_data.tot_pairs - epr_cmd_data.pairs_left

        return epr_cmd_data, pair_index, is_creator, request_key

    def _handle_last_epr_pair(self, epr_cmd_data, is_creator, request_key):
        # Check if this was the last pair
        if epr_cmd_data.pairs_left == 0:
            if is_creator:
                self._epr_create_requests.pop(request_key)
            else:
                self._epr_recv_requests[request_key].pop(0)

    def _store_ent_info(self, epr_cmd_data, response, pair_index):
        self._logger.debug("Storing entanglement information for pair {pair_index}")
        # Store the entanglement information
        ent_info = [entry.value if isinstance(entry, Enum) else entry for entry in response]
        ent_info_array_address = epr_cmd_data.ent_info_array_address
        # Start and stop of slice
        arr_start = pair_index * OK_FIELDS
        arr_stop = (pair_index + 1) * OK_FIELDS
        subroutine_id = epr_cmd_data.subroutine_id
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        self._app_arrays[app_id][ent_info_array_address, arr_start:arr_stop] = ent_info

    def _handle_epr_ok_k_response(self, epr_cmd_data, response, pair_index):

        # Extract qubit addresses
        subroutine_id = epr_cmd_data.subroutine_id
        app_id = self._get_app_id(subroutine_id=subroutine_id)
        virtual_address = self._get_virtual_address_from_epr_data(epr_cmd_data, pair_index, app_id)

        # If the virtual address is currently in use, we should wait
        if self._has_virtual_address(app_id=app_id, virtual_address=virtual_address):
            self._logger.debug(f"Since virtual address {virtual_address} is in use, "
                               "handling of epr will wait and try again.")
            return False

        # Update qubit mapping
        physical_address = response.logical_qubit_id
        self._logger.debug(f"Virtual qubit address {virtual_address} will now be mapped to "
                           f"physical address {physical_address}")
        self._allocate_physical_qubit(
            subroutine_id=subroutine_id,
            virtual_address=virtual_address,
            physical_address=physical_address,
        )

        return True

    def _get_virtual_address_from_epr_data(self, epr_cmd_data, pair_index, app_id):
        q_array_address = epr_cmd_data.q_array_address
        array_entry = parse_address(f"@{q_array_address}[{pair_index}]")
        virtual_address = self._get_array_entry(app_id=app_id, array_entry=array_entry)
        return virtual_address

    def _handle_epr_ok_m_response(self, epr_cmd_data, response, pair_index):
        # M request are always handled
        return True

    def _handle_epr_ok_r_response(self, response):
        raise NotImplementedError
        return True
