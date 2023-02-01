from enum import Enum
from typing import Any, Dict, Iterator, List, Optional, Tuple, Type, Union

from ape.api import BlockAPI, EcosystemAPI, ReceiptAPI, TransactionAPI
from ape.api.networks import ProxyInfoAPI
from ape.contracts import ContractContainer
from ape.types import AddressType, ContractLog, RawAddress
from ape.utils import EMPTY_BYTES32, to_int
from eth_utils import is_0x_prefixed
from ethpm_types import ContractType
from ethpm_types.abi import ConstructorABI, EventABI, EventABIType, MethodABI
from hexbytes import HexBytes
from pydantic import Field, validator
from starknet_py.net.client_models import StarknetBlock as StarknetClientBlock
from starknet_py.net.models.address import parse_address
from starknet_py.net.models.chains import StarknetChainId
from starknet_py.utils.data_transformer.execute_transformer import FunctionCallSerializer
from starkware.starknet.core.os.class_hash import compute_class_hash
from starkware.starknet.definitions.transaction_type import TransactionType
from starkware.starknet.public.abi import get_selector_from_name, get_storage_var_address
from starkware.starknet.public.abi_structs import identifier_manager_from_abi
from starkware.starknet.services.api.contract_class import ContractClass

from ape_starknet.exceptions import (
    ContractTypeNotFoundError,
    StarknetEcosystemError,
    StarknetProviderError,
)
from ape_starknet.transactions import (
    ContractDeclaration,
    DeclareTransaction,
    DeployAccountReceipt,
    DeployAccountTransaction,
    InvokeFunctionReceipt,
    InvokeFunctionTransaction,
    StarknetReceipt,
    StarknetTransaction,
)
from ape_starknet.utils import (
    EXECUTE_ABI,
    STARKNET_FEE_TOKEN_SYMBOL,
    get_method_abi_from_selector,
    to_checksum_address,
)
from ape_starknet.utils.basemodel import StarknetBase

NETWORKS = {
    # chain_id, network_id
    "mainnet": (StarknetChainId.MAINNET.value, StarknetChainId.MAINNET.value),
    "testnet": (StarknetChainId.TESTNET.value, StarknetChainId.TESTNET.value),
    "testnet2": (StarknetChainId.TESTNET2.value, StarknetChainId.TESTNET.value),
}
OZ_PROXY_STORAGE_KEY = get_storage_var_address("Proxy_implementation_hash")


class ProxyType(Enum):
    LEGACY = 0
    ARGENT_X = 1
    OPEN_ZEPPELIN = 2


class StarknetProxy(ProxyInfoAPI):
    """
    A proxy contract in Starknet.
    """

    type: ProxyType


class StarknetBlock(BlockAPI):
    hash: Optional[int] = None
    parent_hash: Any = Field(to_int(EMPTY_BYTES32), alias="parentHash")

    @validator("hash", "parent_hash", pre=True)
    def validate_hexbytes(cls, value):
        if not isinstance(value, int):
            return to_int(value)

        return value


class Starknet(EcosystemAPI, StarknetBase):
    """
    The Starknet ``EcosystemAPI`` implementation.
    """

    fee_token_symbol: str = STARKNET_FEE_TOKEN_SYMBOL

    proxy_info_cache: Dict[AddressType, Optional[StarknetProxy]] = {}

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"

    @classmethod
    def decode_address(cls, raw_address: RawAddress) -> AddressType:
        """
        Make a checksum address given a supported format.
        Borrowed from ``eth_utils.to_checksum_address()`` but supports
        non-length 42 addresses.
        Args:
            raw_address (Union[int, str, bytes]): The value to convert.
        Returns:
            ``AddressType``: The converted address.
        """
        return to_checksum_address(raw_address)

    @classmethod
    def encode_address(cls, address: Union[AddressType, str]) -> int:
        return parse_address(address)

    def serialize_transaction(self, transaction: TransactionAPI) -> bytes:
        if not isinstance(transaction, StarknetTransaction):
            raise StarknetEcosystemError(f"Can only serialize '{StarknetTransaction.__name__}'.")

        starknet_object = transaction.as_starknet_object()
        return starknet_object.deserialize()

    def decode_returndata(self, abi: MethodABI, raw_data: List[int]) -> Any:  # type: ignore
        if not raw_data:
            return raw_data

        full_abi = [
            a.dict() for a in (abi.contract_type.abi if abi.contract_type is not None else [abi])
        ]
        call_serializer = FunctionCallSerializer(abi.dict(), identifier_manager_from_abi(full_abi))
        raw_data = [self.encode_primitive_value(v) for v in raw_data]
        decoded = call_serializer.to_python(raw_data)

        # Keep only the expected data instead of a 1-item array
        if len(abi.outputs) == 1 or (
            len(abi.outputs) == 2 and str(abi.outputs[1].type).endswith("*")
        ):
            decoded = decoded[0]

        return decoded

    def encode_calldata(self, abi: Union[ConstructorABI, MethodABI], *args) -> List:  # type: ignore
        full_abi = abi.contract_type.abi if abi.contract_type is not None else [abi]
        return self._encode_calldata(full_abi=full_abi, abi=abi, call_args=args)

    def _encode_calldata(
        self,
        full_abi: List,
        abi: Union[ConstructorABI, MethodABI],
        call_args,
    ) -> List:
        full_abi = [abi.dict() if hasattr(abi, "dict") else abi for abi in full_abi]
        call_serializer = FunctionCallSerializer(abi.dict(), identifier_manager_from_abi(full_abi))
        pre_encoded_args: List[Any] = []
        index = 0
        last_index = min(len(abi.inputs), len(call_args)) - 1
        did_process_array_during_arr_len = False

        for call_arg, input_type in zip(call_args, abi.inputs):
            if str(input_type.type).endswith("*"):
                if did_process_array_during_arr_len:
                    did_process_array_during_arr_len = False
                    continue

                encoded_arg = self._pre_encode_value(call_arg)
                pre_encoded_args.append(encoded_arg)
            elif (
                input_type.name is not None
                and input_type.name.endswith("_len")
                and index < last_index
                and str(abi.inputs[index + 1].type).endswith("*")
            ):
                pre_encoded_arg = self._pre_encode_value(call_arg)

                if isinstance(pre_encoded_arg, int):
                    # A '_len' arg was provided.
                    array_index = index + 1
                    pre_encoded_array = self._pre_encode_array(call_args[array_index])
                    pre_encoded_args.append(pre_encoded_array)
                    did_process_array_during_arr_len = True
                else:
                    pre_encoded_args.append(pre_encoded_arg)

            else:
                pre_encoded_args.append(self._pre_encode_value(call_arg))

            index += 1

        calldata, _ = call_serializer.from_python(*pre_encoded_args)
        return list(calldata)

    def _pre_encode_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return self._pre_encode_struct(value)
        elif isinstance(value, (list, tuple)):
            return self._pre_encode_array(value)
        else:
            return self.encode_primitive_value(value)

    def _pre_encode_array(self, array: Any) -> List:
        if not isinstance(array, (list, tuple)):
            # Will handle single item structs and felts.
            return self._pre_encode_array([array])

        encoded_array = []
        for item in array:
            encoded_value = self._pre_encode_value(item)
            encoded_array.append(encoded_value)

        return encoded_array

    def _pre_encode_struct(self, struct: Dict) -> Dict:
        encoded_struct = {}
        for key, value in struct.items():
            encoded_struct[key] = self._pre_encode_value(value)

        return encoded_struct

    def encode_primitive_value(self, value: Any) -> int:
        if isinstance(value, bool):
            # NOTE: bool must come before int.
            return int(value)

        elif isinstance(value, int):
            return value

        elif isinstance(value, str) and is_0x_prefixed(value):
            return int(value, 16)

        elif isinstance(value, HexBytes):
            return int(value.hex(), 16)

        return value

    def decode_receipt(self, data: dict) -> ReceiptAPI:
        txn_type = TransactionType(data["transaction"].type)
        receipt_cls: Type[StarknetReceipt]
        if txn_type == TransactionType.INVOKE_FUNCTION:
            receipt_cls = InvokeFunctionReceipt
        elif txn_type == TransactionType.DECLARE:
            receipt_cls = ContractDeclaration
        elif txn_type == TransactionType.DEPLOY_ACCOUNT:
            receipt_cls = DeployAccountReceipt
        else:
            raise StarknetProviderError(f"Unable to handle contract type '{txn_type.value}'.")

        receipt = receipt_cls.parse_obj(data)
        if receipt is None:
            raise StarknetProviderError("Failed to parse receipt from data.")

        return receipt

    def decode_block(self, block: StarknetClientBlock) -> BlockAPI:
        return StarknetBlock(
            hash=block.block_hash,
            number=block.block_number,
            parentHash=block.parent_block_hash,
            size=len(block.transactions),  # TODO: Figure out size
            timestamp=block.timestamp,
        )

    def encode_deployment(
        self, deployment_bytecode: HexBytes, abi: ConstructorABI, *args, **kwargs
    ) -> TransactionAPI:
        contract_class = ContractClass.deserialize(deployment_bytecode)
        class_hash = compute_class_hash(contract_class)
        contract_type = abi.contract_type
        if not contract_type:
            raise StarknetEcosystemError(
                "Unable to encode deployment - missing full contract type for constructor."
            )

        constructor_arguments = self._encode_calldata(contract_type.abi, abi, args)
        return self.universal_deployer.create_deploy(class_hash, constructor_arguments, **kwargs)

    def encode_transaction(
        self, address: AddressType, abi: MethodABI, *args, **kwargs
    ) -> TransactionAPI:
        # NOTE: This method only works for invoke-transactions
        contract_type = abi.contract_type or self.starknet_explorer.get_contract_type(address)
        if not contract_type:
            raise ContractTypeNotFoundError(address)

        arguments = list(args)
        encoded_calldata = self._encode_calldata(contract_type.abi, abi, arguments)
        return InvokeFunctionTransaction(
            receiver=address,
            method_abi=abi,
            calldata=encoded_calldata,
            sender=kwargs.get("sender"),
            max_fee=kwargs.get("max_fee") or 0,
            signature=None,
        )

    def encode_contract_blueprint(
        self, contract: Union[ContractContainer, ContractType], *args, **kwargs
    ) -> DeclareTransaction:
        contract_type = (
            contract.contract_type if isinstance(contract, ContractContainer) else contract
        )
        code = (
            (contract_type.deployment_bytecode.bytecode or 0)
            if contract_type.deployment_bytecode
            else 0
        )
        starknet_contract = ContractClass.deserialize(HexBytes(code))
        return DeclareTransaction(
            contract_type=contract_type, data=starknet_contract.serialize(), **kwargs
        )

    def create_transaction(self, **kwargs) -> TransactionAPI:
        txn_type = TransactionType(kwargs.pop("type", kwargs.pop("tx_type", "")))
        txn_cls: Type[StarknetTransaction]
        invoking = txn_type == TransactionType.INVOKE_FUNCTION
        if invoking:
            txn_cls = InvokeFunctionTransaction
        elif txn_type == TransactionType.DECLARE:
            txn_cls = DeclareTransaction
        elif txn_type == TransactionType.DEPLOY_ACCOUNT:
            txn_cls = DeployAccountTransaction

        txn_data: Dict[str, Any] = {**kwargs, "signature": None}
        if "chain_id" not in txn_data and self.network_manager.active_provider:
            txn_data["chain_id"] = self.provider.chain_id

        # For deploy-txns, 'contract_address' is the address of the newly deployed contract.
        if "contract_address" in txn_data:
            contract_address = self.decode_address(txn_data["contract_address"])
            txn_data["contract_address"] = contract_address
            contract_type = None

            if "class_hash" in txn_data:
                contract_type = self.get_local_contract_type(txn_data["class_hash"])

            if not contract_type:
                contract_type = self.get_contract_type(contract_address)

            if contract_type:
                bytecode_obj = contract_type.deployment_bytecode
                if bytecode_obj:
                    bytecode = bytecode_obj.bytecode
                    txn_data["data"] = bytecode

        if not invoking:
            return txn_cls(**txn_data)

        """ ~ Invoke transactions ~ """

        if not txn_data.get("method_abi") and txn_data.get("entry_point_selector"):
            target_address = self.decode_address(txn_data["contract_address"])
            target_contract_type = self.chain_manager.contracts.get(target_address)
            if not target_contract_type:
                raise StarknetEcosystemError(f"Contract '{target_address}' not found.")

            selector = txn_data["entry_point_selector"]
            txn_data["method_abi"] = get_method_abi_from_selector(selector, target_contract_type)

        else:
            # Assume __execute__
            txn_data["method_abi"] = EXECUTE_ABI

        if "calldata" in txn_data and txn_data["calldata"] is not None:
            # Transactions in blocks show calldata as flattened hex-strs
            # but elsewhere we expect flattened ints. Convert to ints for
            # consistency and testing purposes.
            encoded_calldata = [self.encode_primitive_value(v) for v in txn_data["calldata"]]
            txn_data["calldata"] = encoded_calldata

        if "contract_address" in txn_data:
            txn_data["receiver"] = txn_data.pop("contract_address")

        return txn_cls(**txn_data)

    def decode_logs(self, logs: List[Dict], *events: EventABI) -> Iterator["ContractLog"]:
        events_by_selector = {get_selector_from_name(e.name): e for e in events}
        log_map = {s: [log for log in logs if s in log["keys"]] for s in events_by_selector}

        def from_uint(low: int, high: int) -> int:
            return low + (high << 128)

        def decode_items(
            abi_types: List[EventABIType], data: List[int]
        ) -> List[Union[int, Tuple[int, int]]]:
            decoded: List[Union[int, Tuple[int, int]]] = []
            iter_data = iter(data)
            for abi_type in abi_types:
                if abi_type.type == "Uint256":
                    # Uint256 are stored using 2 slots
                    next_item_1 = next(iter_data, None)
                    next_item_2 = next(iter_data, None)
                    if next_item_1 is not None and next_item_2 is not None:
                        decoded.append(from_uint(next_item_1, next_item_2))
                else:
                    next_item = next(iter_data, None)
                    if next_item:
                        decoded.append(next_item)

            return decoded

        for index, (selector, logs) in enumerate(log_map.items()):
            abi = events_by_selector[selector]
            if not logs:
                continue

            for log in logs:
                event_args = dict(
                    zip([a.name for a in abi.inputs], decode_items(abi.inputs, log["data"]))
                )
                yield ContractLog(
                    block_hash=log["block_hash"],
                    block_number=log["block_number"],
                    contract_address=self.decode_address(log["from_address"]),
                    event_arguments=event_args,
                    event_name=abi.name,
                    log_index=index,
                    transaction_hash=log["transaction_hash"],
                    transaction_index=0,  # Not available
                )

    def get_proxy_info(self, address: AddressType) -> Optional[StarknetProxy]:
        # Proxies are handled elsewhere in Starknet due to ecosystem differences
        # (namely, contract classes function different than proxy contracts in Ethereum).
        return None

    def _get_proxy_info(
        self, address: AddressType, contract_type: ContractType
    ) -> Optional[StarknetProxy]:
        proxy_type: Optional[ProxyType] = None
        target: Optional[int] = None
        instance = self.chain_manager.contracts.instance_at(address, contract_type=contract_type)
        # Legacy proxy check
        if "implementation" in contract_type.view_methods:
            target = instance.implementation()
            proxy_type = ProxyType.LEGACY

        # Argent-X proxy check
        elif "get_implementation" in contract_type.view_methods:
            target = instance.get_implementation()
            proxy_type = ProxyType.ARGENT_X

        # OpenZeppelin proxy check
        elif self.provider.client is not None:
            address_int = self.encode_address(address)
            target = self.provider.client.get_storage_at_sync(
                contract_address=address_int, key=OZ_PROXY_STORAGE_KEY
            )
            if target == "0x0":
                target = None
            else:
                proxy_type = ProxyType.OPEN_ZEPPELIN

        return (
            StarknetProxy(target=self.decode_address(target), type=proxy_type)
            if target and proxy_type
            else None
        )

    def decode_primitive_value(self, value: Any, output_type: Union[str, Tuple, List]) -> int:
        return to_int(value)

    def decode_calldata(self, abi: Union[ConstructorABI, MethodABI], calldata: bytes) -> Dict:
        raise NotImplementedError()
