
from quarkchain.config import DEFAULT_ENV
from quarkchain.core import Address, Transaction, Code
from quarkchain.evm.transactions import Transaction as EvmTransaction


def get_test_env(
        genesisAccount=Address.createEmptyAccount(),
        genesisQuarkash=0,
        genesisMinorQuarkash=0):
    env = DEFAULT_ENV.copy()
    env.config.setShardSize(2)
    env.config.SKIP_MINOR_DIFFICULTY_CHECK = True
    env.config.SKIP_ROOT_DIFFICULTY_CHECK = True
    env.config.SKIP_MINOR_COINBASE_CHECK = False
    env.config.GENESIS_ACCOUNT = genesisAccount
    env.config.GENESIS_COIN = genesisQuarkash
    env.config.GENESIS_MINOR_COIN = genesisMinorQuarkash
    env.config.TESTNET_MASTER_ACCOUNT = genesisAccount
    return env


def create_transfer_transaction(
        shardState,
        fromId,
        toAddress,
        amount,
        startgas=21000,     # transfer tx min gas
        gasprice=1):
    """ Create an in-shard xfer tx
    """
    evmTx = EvmTransaction(
        branchValue=shardState.branch.value,
        nonce=shardState.evmState.get_nonce(fromId.getRecipient()),
        gasprice=gasprice,
        startgas=startgas,
        to=toAddress.recipient,
        value=amount,
        data=b'')
    evmTx.sign(
        key=fromId.getKey(),
        network_id=shardState.env.config.NETWORK_ID)
    return Transaction(
        inList=[],
        code=Code.createEvmCode(evmTx),
        outList=[])