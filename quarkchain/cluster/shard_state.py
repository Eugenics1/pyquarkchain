from quarkchain.core import calculate_merkle_root, Constant, uint256
from quarkchain.core import PreprendedSizeBytesSerializer, PreprendedSizeListSerializer
from quarkchain.evm.state import State as EvmState
from quarkchain.reward import ConstMinorBlockRewardCalcultor
from quarkchain.evm import opcodes
from quarkchain.evm.messages import apply_transaction
from quarkchain.config import NetworkId
from quarkchain.utils import Logger
from quarkchain.cluster.core import RootBlock
from quarkchain.utils import check
from quarkchain.cluster.genesis import create_genesis_minor_block, create_genesis_root_block


class CrossShardTransactionDeposit:
    """ Destination of x-shard tx
    """
    FIELDS = (
        ("recipient", PreprendedSizeBytesSerializer(20)),
        ("amount", uint256),
        ("gasUsed", uint256),
        ("gasPrice", uint256),
    )

    def __init__(self, recipient, amount, gasUsed, gasPrice):
        self.recipient = recipient
        self.amount = amount
        self.gasUsed = gasUsed
        self.gasPrice = gasPrice


class CrossShardTransactionList:
    FIELDS = (
        ("txList", PreprendedSizeListSerializer(4, CrossShardTransactionDeposit))
    )

    def __init__(self, txList):
        self.txList = txList


class ShardDb:
    def __init__(self, db):
        self.db = db
        # TODO:  iterate db to recover pools and set
        self.mHeaderPool = dict()
        self.mMetaPool = dict()
        self.xShardSet = set()
        self.rHeaderPool = dict()

    def putRootBlock(self, rootBlock, rootBlockHash=None):
        if rootBlockHash is None:
            rootBlockHash = rootBlock.header.getHash()

        self.db.put(b"rblock_" + rootBlockHash, rootBlock.serialize())
        self.rHeaderPool[rootBlockHash] = rootBlock.header

    def getRootBlockByHash(self, h):
        return RootBlock.deserialize(self.db.get(b"rblock_" + h))

    def getRootBlockHeaderByHash(self, h):
        return self.rHeaderPool.get(h)

    def putMinorBlock(self, mBlock, evmState, mBlockHash=None):
        if mBlockHash is None:
            mBlockHash = mBlock.header.getHash()

        self.db.put(b"mblock_" + mBlockHash, mBlock.serialize())
        self.db.put(b"state_" + mBlockHash, evmState.trie.root_hash)
        self.mHeaderPool[mBlockHash] = mBlock.header
        self.mMetaPool[mBlockHash] = mBlock.meta

    def getMinorBlockHeaderByHash(self, h):
        return self.mHeaderPool.get(h)

    def getMinorBlockEvmRootHashByBlockHash(self, h):
        return self.db.get(b"state_" + h)

    def containMinorBlockByHash(self, h):
        return h in self.mHeaderPool

    def putMinorBlockXshardTxList(self, h, txList):
        self.xShardSet.add(h)
        self.db.put(b"xShard_" + h, txList.serialize())

    def getMinorBlockXshardTxList(self, h):
        return CrossShardTransactionList.deserialize(self.db.get(b"xShard_" + h))

    def put(self, key, value):
        self.db.put(key, value)

    def get(self, key, default=None):
        return self.db.get(key, default)

    def __getitem__(self, key):
        return self[key]


class ShardState:
    """  State of a shard, which includes
    - evm state
    - minor blockchain
    - root blockchain and cross-shard transaction
    And we can perform state change either by append new block or roll back a block
    TODO: Support
    - reshard by split
    """

    def __init__(self, env, shardId, createGenesis=False, db=None):
        self.env = env
        self.diffCalc = self.env.config.MINOR_DIFF_CALCULATOR
        self.diffHashFunc = self.env.config.DIFF_HASH_FUNC
        self.rewardCalc = ConstMinorBlockRewardCalcultor(env)
        self.rawDb = db if db is not None else env.db
        self.db = ShardDb(self.rawDb)

        check(createGenesis)
        if createGenesis:
            self.__createGenesisBlocks(shardId)

        # TODO: Query db to recover the latest state

    def __createGenesisBlocks(self, shardId):
        genesisRootBlock = create_genesis_root_block(self.env)
        genesisMinorBlock = create_genesis_minor_block(
            env=self.env,
            shardId=shardId,
            hashRootBlock=genesisRootBlock.header.getHash())

        self.evmState = EvmState(env=self.env.evmEnv, db=self.db)
        self.evmState.block_coinbase = genesisMinorBlock.meta.coinbaseAddress.recipient
        self.evmState.delta_balance(
            self.evmState.block_coinbase,
            self.env.config.GENESIS_MINOR_COIN)
        self.evmState.commit()

        self.branch = genesisMinorBlock.header.branch
        self.db.putMinorBlock(genesisMinorBlock, self.evmState)
        self.db.putRootBlock(genesisRootBlock)

        self.rootTip = genesisRootBlock
        self.shardTip = genesisMinorBlock

    def __performTx(self, tx, evmState):
        # UTXOs are not supported now
        if len(tx.inList) != 0:
            raise RuntimeError("input list must be empty")
        if len(tx.outList) != 0:
            raise RuntimeError("output list must be empty")
        if len(tx.signList) != 0:
            raise RuntimeError("sign list must be empty")

        # Check OP code
        if len(tx.code.code) == 0:
            raise RuntimeError("empty op code")
        if not tx.code.isEvm:
            raise RuntimeError("only evm transaction is supported now")

        evmTx = tx.code.getEvmTransaction()
        if self.branch.value != evmTx.branchValue:
            raise RuntimeError("evm tx is not in the shard")
        if evmTx.getWithdraw() < 0:
            raise RuntimeError("withdraw must be non-negative")
        if evmTx.getWithdraw() != 0:
            if len(evmTx.withdrawTo) != Constant.ADDRESS_LENGTH:
                raise RuntimeError("evm withdraw address is incorrect")
            if evmTx.startgas < opcodes.GTXXSHARDCOST:
                raise RuntimeError("insufficient startgas")
            evmTx.startgas -= opcodes.GTXXSHARDCOST
            withdrawCost = opcodes.GTXXSHARDCOST * evmTx.gasprice + evmState.getWithdraw()
            if evmState.get_balance(evmTx.sender) < withdrawCost:
                raise RuntimeError("insufficient balance")
            evmState.delta_balance(evmTx.sender, -withdrawCost)
            # the xshard gas and fee is consumed by destination shard block

        success, output = apply_transaction(evmState, evmTx)
        return success, output

    def __getEvmStateForNewBlock(self, blockHash):
        state = EvmState(env=self.env)
        state.trie.root_hash = self.db.getMinorBlockEvmRootHashByBlockHash(blockHash)
        return state

    def appendBlock(self, block):
        """  Append a block.  This would perform validation check with local
        UTXO pool and perform state change atomically
        Return None upon success, otherwise return a string with error message
        """

        # TODO: May check if the block is already in db (and thus already
        # validated)

        if not self.db.containMinorBlockByHash(block.header.hashPrevMinorBlock):
            # TODO:  May put the block back to queue
            return "prev block not found"
        prevHeader = self.getMinorBlockHeaderByHash(block.header.hashPrevMinorBlock)
        prevMeta = self.getMinorBlockMetaByHash(block.header.hashPrevMinorBlock)

        if block.header.height != prevHeader.height + 1:
            return "height mismatch"

        if block.header.branch != self.branch:
            return "branch mismatch"

        if block.header.createTime <= prevHeader.createTime:
            return "incorrect create time tip time {}, new block time {}".format(
                block.header.createTime, self.chain[-1].createTime)

        if len(block.meta.extraData) > self.env.config.BLOCK_EXTRA_DATA_SIZE_LIMIT:
            return "extraData in block is too large"

        # Make sure merkle tree is valid
        merkleHash = calculate_merkle_root(block.txList)
        if merkleHash != block.meta.hashMerkleRoot:
            return "incorrect merkle root"

        # Check the first transaction of the block
        if not self.branch.isInShard(block.txList[0].outList[0].address.fullShardId):
            return "coinbase output address must be in the shard"

        # Check difficulty
        if not self.env.config.SKIP_MINOR_DIFFICULTY_CHECK:
            if self.env.config.NETWORK_ID == NetworkId.MAINNET:
                diff = self.getNextBlockDifficulty(block.header.createTime)
                metric = diff * int.from_bytes(block.header.getHash(), byteorder="big")
                if metric >= 2 ** 256:
                    return "incorrect difficulty"
            elif block.meta.coinbaseAddress.recipient != self.env.config.TESTNET_MASTER_ACCOUNT.recipient:
                return "incorrect master to create the block"

        if not self.branch.isInShard(block.meta.coinbaseAddress.fullShardId):
            return "coinbase output must be in local shard"

        # Check whether the root header is in the root chain
        rootBlockHeader = self.db.getBlockHeaderByHash(block.header.hashPrevRootBlock)
        if rootBlockHeader is None:
            return "cannot find root block for the minor block"

        if rootBlockHeader.height < self.rootChain.getBlockHeaderByHash(prevMeta.hashPrevRootBlock).height:
            return "prev root block height must be non-decreasing"

        evmState = self.__getEvmStateForNewBlock(prevHeader.getHash())
        evmState.txindex = 0
        evmState.gas_used = 0
        evmState.bloom = 0
        evmState.receipts = []
        evmState.timestamp = block.header.createTime
        evmState.gas_limit = block.meta.gas_limit  # TODO
        evmState.block_number = block.header.height
        evmState.recent_uncles[evmState.block_number] = []  # TODO [x.hash for x in block.uncles]
        # TODO: Create a account with shard info if the account is not created
        evmState.block_coinbase = block.meta.coinbaseAddress.recipient
        evmState.block_difficulty = block.header.difficulty
        evmState.block_reward = 0
        evmState.prev_headers = []                          # TODO: evmState.add_block_header(block.header)

        self.__runCrossShardTxList(evmState, rootBlockHeader, prevMeta.hashPrevRootBlock)

        for idx, tx in enumerate(block.txList):
            txHash = tx.getHash()
            try:
                self.__performTx(tx, rootBlockHeader, evmState, txHash=txHash)
            except Exception as e:
                Logger.errorException()
                Logger.debug("failed to process Tx {}, idx {}, reason {}".format(
                    tx.getHash().hex(), idx, e))
                return str(e)

        # ------------------------ Validate ending result of the block --------------------
        # Update actual root hash
        evmState.commit()
        if block.meta.hashEvmStateRoot != evmState.trie.root_hash:
            raise ValueError("State root mismatch: header %s computed %s" %
                             (block.meta.hashEvmStateRoot.hex(), evmState.trie.root_hash.hex()))

        if evmState.gas_used != block.meta.evmGasUsed:
            raise ValueError("Gas used mismatch: header %d computed %d" %
                             (block.meta.evmGasUsed, evmState.gas_used))

        if evmState.block_reward != block.meta.coinbaseAmount:
            raise ValueError("Coinbase reward incorrect")
        # TODO: Check evm receipt and bloom

        # The rest fee goes to root block
        # TODO: Add block reward to coinbase
        # self.rewardCalc.getBlockReward(self):
        self.db.putMinorBlock(block)

        self.blockPool[block.header.getHash()] = block.header

        return None

    def tip(self):
        """ Return the header of the tail of the shard
        """
        return self.header

    def metaTip(self):
        return self.meta

    def getBlockHeaderByHeight(self, height):
        pass
        # return self.chain[height]

    def getBlockHeaderByHash(self, h):
        return self.blockPool.get(h, None)

    def getGenesisBlock(self):
        return self.genesisBlock

    def getBalance(self, recipient):
        return self.evmState.get_balance(recipient)

    def getNextBlockDifficulty(self, createTime):
        return self.diffCalc.calculateDiff(self, createTime)

    def getNextBlockReward(self):
        return self.rewardCalc.getBlockReward(self)

    def createBlockToAppend(self, createTime=None, address=None):
        """ Create an empty block to append
        """
        block = self.tip().createBlockToAppend(
            createTime=createTime,
            address=address,
            quarkash=self.getNextBlockReward())
        block.header.difficulty = self.getNextBlockDifficulty(block.header.createTime)
        return block

    def createBlockToMine(self, createTime=None, address=None, includeTx=True):
        """ Create a block to append and include TXs to maximize rewards
        """
        pass

    def addTransactionToQueue(self, transaction):
        # TODO: limit transaction queue size
        self.transactionPool.add(transaction, self.utxoPool)

    def getPendingTxSize(self):
        return self.transactionPool.size()

    #
    # ============================ Cross-shard transaction handling =============================
    #
    def addCrossShardTxListByMinorBlockHash(self, h, txList):
        ''' Add a cross shard tx list from another shard
        '''
        self.db.putMinorBlockXshardTxList(h, txList)

    def addRootBlock(self, rBlock):
        ''' Add a root block.
        Make sure all cross shard tx lists confirmed by the root block are in local db.
        '''
        if not self.db.containMinorBlockByHash(rBlock.header.hashPrevRootBlock):
            raise ValueError("cannot find previous root block in pool")

        for mHeader in rBlock.minorBlockHeaderList:
            h = mHeader.getHash()
            if h not in self.xShardSet:
                raise ValueError("cannot find xShard tx list")

        self.db.putRootBlock(rBlock)

    def __getCrossShardTxListByRootBlockHash(self, h):
        rBlock = self.db.getRootBlockByHash(h)
        txList = []
        for mHeader in rBlock.minorBlockHeaderList:
            h = mHeader.getHash()
            txList.extend(self.db.getMinorBlockXshardTxList(h))

        # Apply root block coinbase
        if self.branch.isInShard(rBlock.header.coinbaseAddress.fullShardId):
            txList.append(CrossShardTransactionDeposit(
                recipient=rBlock.header.coinbaseAddress,
                amount=rBlock.header.coinbaseAmount,
                gasUsed=0,
                gasPrice=0))
        return txList

    def __runCrossShardTxList(self, evmState, descendantRootHeader, ancestorRootHeader):
        rHeader = descendantRootHeader
        while rHeader != ancestorRootHeader:
            if rHeader.height == ancestorRootHeader.height:
                raise ValueError(
                    "incorrect ancestor root header: expected {}, actual {}",
                    rHeader.getHash().hex(),
                    ancestorRootHeader.getHash().hex())
            if evmState.gas_used == evmState.gas_limit:
                raise ValueError("gas consumed by cross-shard tx exceeding limit")

            txList = self.__getCrossShardTxListByRootBlockHash(descendantRootHeader.getHash())
            for tx in txList:
                evmState.delta_balance(tx.recipient, tx.amount)
                evmState.gas_used = min(evmState.gas_used + tx.gasUsed, evmState.gas_limit)
                evmState.delta_balance(evmState.block_coinbase, evmState.gas_used * evmState.gas_price // 2)

            rHeader = rHeader.hashPrevRootBlock