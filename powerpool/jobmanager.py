import bitcoinrpc
import struct
import urllib3
import gevent
import signal

from cryptokit.base58 import get_bcaddress_version
from future.utils import viewitems
from binascii import unhexlify, hexlify
from collections import deque
from cryptokit.transaction import Transaction, Input, Output
from cryptokit.block import BlockTemplate
from cryptokit import bits_to_difficulty
from cryptokit.util import pack
from cryptokit.bitcoin import data as bitcoin_data
from gevent import sleep, Greenlet, spawn
from copy import copy


class MonitorNetwork(Greenlet):
    def _set_config(self, **kwargs):
        # A fast way to set defaults for the kwargs then set them as attributes
        self.config = dict(coinserv=None, extranonce_serv_size=8,
                           extranonce_size=4, diff1=0x0000FFFF00000000000000000000000000000000000000000000000000000000,
                           merged=None, block_poll=0.2, job_generate_int=75,
                           rpc_ping_int=2, pow_func='ltc_scrypt', pool_address=None,
                           donate_address=None)
        self.config.update(kwargs)

        if (not get_bcaddress_version(self.config['pool_address']) or
                not get_bcaddress_version(self.config['donate_address'])):
            self.logger.error("No valid donation/pool address configured! Exiting.")
            exit()

        # check that we have at least one configured coin server
        if not self.config['main_coinservs']:
            self.logger.error("Shit won't work without a coinserver to connect to")
            exit()

    def __init__(self, server, **config):
        Greenlet.__init__(self)
        self._set_config(**config)
        self.logger = server.register_logger('jobmanager')

        # start each aux chain monitor for merged mining
        for coin in self.config['merged']:
            if not coin['enabled']:
                self.logger.info("Skipping aux chain support because it's disabled")
                continue

            self.logger.info("Aux network monitor for {} starting up"
                             .format(coin['name']))
            aux_network = MonitorAuxChain(self, **coin)
            aux_network.start()
            self.greenlets.append(("{} Aux network monitor".format(coin['name']), aux_network))
            self.auxmons.append(aux_network)

        # convenient access to global objects
        self.stratum_manager = server.stratum_manager
        self.server = server

        # Aux network monitors (merged mining)
        self.auxmons = []

        # internal vars
        self._last_gbt = None
        self._poll_connection = None
        self._down_connections = []
        self._job_counter = 0
        self._last_aux_update = dict()
        self._node_monitor = None

        self.jobs = {}
        self.live_connections = []
        self.latest_job = None
        self.merged_work = {}
        # general current network stats
        self.current_net = dict(difficulty=None,
                                height=None,
                                subsidy=None)
        self.block_stats = dict(accepts=0,
                                rejects=0,
                                solves=0,
                                last_solve_height=None,
                                last_solve_time=None,
                                last_solve_worker=None)
        self.recent_blocks = deque(maxlen=15)

        for serv in self.config['main_coinservs']:
            conn = bitcoinrpc.AuthServiceProxy(
                "http://{0}:{1}@{2}:{3}/"
                .format(serv['username'],
                        serv['password'],
                        serv['address'],
                        serv['port']),
                pool_kwargs=dict(maxsize=serv.get('maxsize', 10)))
            conn.config = serv
            conn.name = "{}:{}".format(serv['address'], serv['port'])
            self._down_connections.append(conn)

    def call_rpc(self, command, *args, **kwargs):
        try:
            getattr(self.coinserv, command)(*args, **kwargs)
        except (urllib3.exceptions.HTTPError, bitcoinrpc.CoinRPCException) as e:
            self.logger.warn("Unable to perform {} on RPC server. Got: {}"
                             .format(command, e))
            self.down_connection(self._poll_connection)
            raise RPCException(e)

    def down_connection(self, conn):
        """ Called when a connection goes down. Removes if from the list of
        live connections and recomputes a new. """
        if conn in self.live_connections:
            self.live_connections.remove(conn)

        if self._poll_connection is conn:
            # find the next best poll connection
            try:
                self._poll_connection = min(self.live_connections,
                                            key=lambda x: x.config['poll_priority'])
            except ValueError:
                self._poll_connection = None
                self.logger.error("No RPC connections available for polling!!!")
            else:
                self.logger.warn("RPC connection {} switching to poll_connection "
                                 "after {} went down!"
                                 .format(self._poll_connection.name, conn.name))

        if conn not in self._down_connections:
            self.logger.info("Server at {} now reporting down".format(conn.name))
            self._down_connections.append(conn)

    def _monitor_nodes(self):
        while True:
            remlist = []
            for conn in self._down_connections:
                try:
                    conn.getinfo()
                except (urllib3.exceptions.HTTPError, bitcoinrpc.CoinRPCException):
                    self.logger.info("RPC connection {} still down!".format(conn.name))
                    continue

                self.live_connections.append(conn)
                remlist.append(conn)
                self.logger.info("Connected to RPC Server {0}. Yay!".format(conn.name))

                # if this connection has a higher priority than current
                if self._poll_connection is not None:
                    curr_poll = self._poll_connection.config['poll_priority']
                    if conn.config['poll_priority'] > curr_poll:
                        self.logger.info("RPC connection {} has higher poll priority than "
                                         "current poll connection, switching..."
                                         .format(conn.name))
                        self._poll_connection = conn
                else:
                    self._poll_connection = conn
                    self.logger.info("RPC connection {} defaulting poll connection"
                                     .format(conn.name))

            for conn in remlist:
                self._down_connections.remove(conn)

            sleep(self.config['rpc_ping_int'])

    def kill(self, *args, **kwargs):
        """ Override our default kill method and kill our child greenlets as
        well """
        self.logger.info("Network monitoring jobmanager shutting down...")
        self._node_monitor.kill(*args, **kwargs)
        # stop all greenlets
        for name, gl in self.auxmons:
            gl.kill(timeout=self.config['term_timeout'], block=False)
        Greenlet.kill(self, *args, **kwargs)

    def _run(self):
        self.logger.info("Network monitoring jobmanager starting up...")
        # start watching our nodes to see if they're up or not
        self._node_monitor = spawn(self._monitor_nodes)
        i = 0
        while True:
            try:
                if self._poll_connection is None:
                    self.logger.warn("Couldn't connect to any RPC servers, sleeping for 1")
                    sleep(1)
                    continue

                # if there's a new block registered
                if self.check_height():
                    self.logger.info("New block on main network detected")
                    # dump the current transaction pool, refresh and push the
                    # event
                    self.getblocktemplate(new_block=True)
                else:
                    # check for new transactions when count interval has passed
                    if i >= self.config['job_generate_int']:
                        i = 0
                        self.getblocktemplate()
                    i += 1
            except Exception:
                self.logger.error("Unhandled exception!", exc_info=True)
                pass

            sleep(self.config['block_poll'])

    def check_height(self):
        # check the block height
        try:
            height = self._poll_connection.getblockcount()
        except Exception:
            self.logger.warn("Unable to communicate with server that thinks it's live.")
            self.down_connection(self._poll_connection)
            return False

        if self.current_net['height'] != height:
            self.current_net['height'] = height
            return True
        return False

    def getblocktemplate(self, new_block=False, signal=False):
        if signal:
            print "Generating new job from signal!"
        dirty = False
        try:
            # request local memory pool and load it in
            bt = self._poll_connection.getblocktemplate(
                {'capabilities': [
                    'coinbasevalue',
                    'coinbase/append',
                    'coinbase',
                    'generation',
                    'time',
                    'transactions/remove',
                    'prevblock',
                ]})
        except Exception as e:
            self.logger.warn("Failed to fetch new job. Reason: {}".format(e))
            self._down_connection(self._poll_connection)
            return False

        # generate a new job if we got some new work!
        if bt != self._last_gbt:
            self._last_gbt = bt
            dirty = True

        if new_block or dirty:
            # generate a new job and push it if there's a new block on the
            # network
            self.generate_job(push=new_block, flush=new_block, new_block=new_block)

    def generate_job(self, push=False, flush=False, new_block=False):
        """ Creates a new job for miners to work on. Push will trigger an
        event that sends new work but doesn't force a restart. If flush is
        true a job restart will be triggered. """

        # aux monitors will often call this early when not needed at startup
        if self._last_gbt is None:
            return

        if self.merged_work:
            tree, size = bitcoin_data.make_auxpow_tree(self.merged_work)
            mm_hashes = [self.merged_work.get(tree.get(i), dict(hash=0))['hash']
                         for i in xrange(size)]
            mm_data = '\xfa\xbemm'
            mm_data += bitcoin_data.aux_pow_coinbase_type.pack(dict(
                merkle_root=bitcoin_data.merkle_hash(mm_hashes),
                size=size,
                nonce=0,
            ))
            mm_later = [(aux_work, mm_hashes.index(aux_work['hash']), mm_hashes)
                        for chain_id, aux_work in self.merged_work.iteritems()]
        else:
            mm_later = []
            mm_data = None

        # here we recalculate the current merkle branch and partial
        # coinbases for passing to the mining clients
        coinbase = Transaction()
        coinbase.version = 2
        # create a coinbase input with encoded height and padding for the
        # extranonces so script length is accurate
        extranonce_length = (self.config['extranonce_size'] +
                             self.config['extranonce_serv_size'])
        coinbase.inputs.append(
            Input.coinbase(self._last_gbt['height'],
                           addtl_push=[mm_data] if mm_data else [],
                           extra_script_sig=b'\0' * extranonce_length))
        # simple output to the proper address and value
        coinbase.outputs.append(
            Output.to_address(self._last_gbt['coinbasevalue'], self.config['pool_address']))
        job_id = hexlify(struct.pack(str("I"), self._job_counter))
        self.logger.info("Generating new block template with {} trans. Diff {}. Subsidy {}."
                         .format(len(self._last_gbt['transactions']),
                                 bits_to_difficulty(self._last_gbt['bits']),
                                 self._last_gbt['coinbasevalue']))
        bt_obj = BlockTemplate.from_gbt(self._last_gbt,
                                        coinbase,
                                        extranonce_length,
                                        [Transaction(unhexlify(t['data']), fees=t['fee'])
                                         for t in self._last_gbt['transactions']])
        bt_obj.mm_later = copy(mm_later)
        hashes = [bitcoin_data.hash256(tx.raw) for tx in bt_obj.transactions]
        bt_obj.merkle_link = bitcoin_data.calculate_merkle_link([None] + hashes, 0)
        bt_obj.job_id = job_id
        bt_obj.block_height = self._last_gbt['height']
        bt_obj.acc_shares = set()

        if push:
            if flush:
                self.logger.info("New work announced! Wiping previous jobs...")
                self.jobs.clear()
                self.latest_job = None
            else:
                self.logger.info("New work announced!")

        self._job_counter += 1
        self.jobs[job_id] = bt_obj
        self.latest_job = job_id
        if push:
            for idx, client in viewitems(self.stratum_manager.clients):
                try:
                    if flush:
                        client.new_block_event.set()
                    else:
                        client.new_work_event.set()
                except AttributeError:
                    pass

        if new_block:
            hex_bits = hexlify(bt_obj.bits)
            self.current_net['difficulty'] = bits_to_difficulty(hex_bits)


class RPCException(Exception):
    pass


class MonitorAuxChain(Greenlet):
    def __init__(self, server, **kwargs):
        Greenlet.__init__(self)
        self.netmon = server.netmon
        self.server = server
        self.config = server.config
        self.__dict__.update(kwargs)
        self.logger = server.register_self.logger('auxmonitor_{}'
                                                  .format(self.name))
        self.state = {'difficulty': None,
                      'height': None,
                      'chain_id': None,
                      'block_solve': None,
                      'work_restarts': 0,
                      'new_jobs': 0,
                      'solves': 0,
                      'rejects': 0,
                      'accepts': 0,
                      'recent_blocks': deque(maxlen=15)}

        self.coinservs = self.coinserv
        self.coinserv = bitcoinrpc.AuthServiceProxy(
            "http://{0}:{1}@{2}:{3}/"
            .format(self.coinserv[0]['username'],
                    self.coinserv[0]['password'],
                    self.coinserv[0]['address'],
                    self.coinserv[0]['port']),
            pool_kwargs=dict(maxsize=self.coinserv[0].get('maxsize', 10)))
        self.coinserv.config = self.coinservs[0]

        if self.signal:
            gevent.signal(self.signal, self.update, reason="Signal recieved")

    def call_rpc(self, command, *args, **kwargs):
        try:
            getattr(self.coinserv, command)(*args, **kwargs)
        except (urllib3.exceptions.HTTPError, bitcoinrpc.CoinRPCException) as e:
            self.logger.warn("Unable to perform {} on RPC server. Got: {}"
                             .format(command, e))
            raise RPCException(e)

    def update(self, reason=None):
        if reason:
            self.logger.info("Updating {} aux work from a signal recieved!"
                             .format(self.name))

        # cheap hack to prevent a race condition...
        if self.netmon._poll_connection is None:
            self.logger.warn("Couldn't connect to any RPC servers, sleeping for 1")
            sleep(1)
            return False

        try:
            auxblock = self.coinserv.getauxblock()
        except RPCException:
            sleep(2)
            return False

        #self.logger.debug("Aux RPC returned: {}".format(auxblock))
        new_merged_work = dict(
            hash=int(auxblock['hash'], 16),
            target=pack.IntType(256).unpack(auxblock['target'].decode('hex')),
            merged_proxy=self.coinserv,
            monitor=self
        )
        self.state['chain_id'] = auxblock['chainid']
        if new_merged_work != self.netmon.merged_work.get(auxblock['chainid']):
            try:
                height = self.coinserv.getblockcount()
            except RPCException:
                sleep(2)
                return False
            self.logger.info("New aux work announced! Diff {}. RPC returned: {}"
                             .format(bitcoin_data.target_to_difficulty(new_merged_work['target']),
                                     new_merged_work))
            self.netmon.merged_work[auxblock['chainid']] = new_merged_work
            self.state['difficulty'] = bitcoin_data.target_to_difficulty(pack.IntType(256).unpack(auxblock['target'].decode('hex')))
            # only push the job if there's a new block height discovered.
            if self.state['height'] != height:
                self.state['height'] = height
                self.netmon.generate_job(push=True, flush=self.flush)
                self.state['work_restarts'] += 1
            else:
                self.monitor_network.generate_job()
                self.state['new_jobs'] += 1

    def kill(self, *args, **kwargs):
        """ Override our default kill method and kill our child greenlets as
        well """
        self.logger.info("Auxilury network monitor for {} shutting down..."
                         .format(self.name))
        Greenlet.kill(self, *args, **kwargs)

    def _run(self):
        self.logger.info("Auxilury network monitor for {} starting up..."
                         .format(self.name))
        while True:
            if not self.update():
                continue
            sleep(self.work_interval)
