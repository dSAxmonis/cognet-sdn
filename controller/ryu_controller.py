# # # """
# # # CogNet-SDN | controller/ryu_controller.py

# # # Ryu OpenFlow 1.3 controller with:
# # #   - MAC learning + proactive flow installation
# # #   - Port stats collection (bandwidth / throughput)
# # #   - OpenFlow echo-based delay measurement
# # #   - Packet-loss calculation
# # #   - 6×6 Traffic Matrix (state for DQN)
# # #   - REST API on port 8181

# # # Run:
# # #   ~/sdnenv/bin/ryu-manager \
# # #       --ofp-tcp-listen-port 6653 \
# # #       --wsapi-port 8181 \
# # #       ~/cognet-sdn/controller/ryu_controller.py

# # # REST endpoints:
# # #   GET  /cognet/stats/links          → per-link BW, delay, loss
# # #   GET  /cognet/stats/traffic_matrix → 6×6 TM (flat list, 36 values)
# # #   GET  /cognet/stats/switches       → connected switch DPIDs
# # #   POST /cognet/flow/install         → install a flow rule manually
# # #   GET  /cognet/topo                 → switch-to-switch adjacency
# # # """

# # # import json
# # # import time
# # # import threading
# # # from collections import defaultdict

# # # from ryu.base import app_manager
# # # from ryu.controller import ofp_event
# # # from ryu.controller.handler import (CONFIG_DISPATCHER,
# # #                                     MAIN_DISPATCHER,
# # #                                     set_ev_cls)
# # # from ryu.ofproto import ofproto_v1_3
# # # from ryu.lib.packet import packet, ethernet, ether_types, lldp, ipv4, arp
# # # from ryu.lib import hub
# # # from ryu.app.wsgi import (ControllerBase, WSGIApplication,
# # #                            route, Response)
# # # from ryu.topology.api import get_switch, get_link

# # # # ── Constants ──────────────────────────────────────────────────────────────
# # # NUM_SWITCHES    = 6          # s1 … s6
# # # STATS_INTERVAL  = 5          # seconds — DTPRO prediction interval Pi
# # # FLOW_PRIORITY   = 1
# # # TABLE_MISS_PRI  = 0
# # # ALPHA           = 0.3        # reward weight: throughput  (DTPRO best params)
# # # BETA            = 1.0        # reward weight: latency
# # # GAMMA_R         = 1.0        # reward weight: packet loss

# # # APP_NAME = 'cognet_controller'


# # # class CogNetController(app_manager.RyuApp):
# # #     OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
# # #     _CONTEXTS   = {'wsgi': WSGIApplication}

# # #     def __init__(self, *args, **kwargs):
# # #         super().__init__(*args, **kwargs)

# # #         # ── State ──────────────────────────────────────────────────────────
# # #         self.mac_to_port   = {}      # {dpid: {mac: port}}
# # #         self.datapaths     = {}      # {dpid: datapath}
# # #         self.topo_links    = {}      # {(src_dpid, dst_dpid): (src_port, dst_port)}

# # #         # Per-port stats snapshots  {dpid: {port: {tx_bytes, rx_bytes, ts}}}
# # #         self._port_prev    = defaultdict(dict)
# # #         self._port_curr    = defaultdict(dict)

# # #         # Derived metrics  {dpid: {port: {bw_mbps, loss_ratio, delay_ms}}}
# # #         self.link_stats    = defaultdict(dict)

# # #         # Traffic Matrix [NUM_SWITCHES × NUM_SWITCHES] — flat list
# # #         self.traffic_matrix = [0.0] * (NUM_SWITCHES * NUM_SWITCHES)

# # #         # Echo-based delay  {dpid: delay_ms}
# # #         self.switch_delay  = defaultdict(float)
# # #         self._echo_ts      = {}      # {dpid: send_timestamp}

# # #         # Lock for shared data
# # #         self._lock = threading.Lock()

# # #         # Register REST API
# # #         wsgi = kwargs['wsgi']
# # #         wsgi.register(CogNetRestAPI, {APP_NAME: self})

# # #         # Start background stats poller
# # #         self.monitor_thread = hub.spawn(self._monitor_loop)

# # #         self.logger.info("CogNet-SDN Controller started  (port 6653 / REST 8181)")

# # #     # ── OpenFlow Handshake ─────────────────────────────────────────────────
# # #     @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
# # #     def switch_features_handler(self, ev):
# # #         dp    = ev.msg.datapath
# # #         ofp   = dp.ofproto
# # #         parser = dp.ofproto_parser

# # #         self.datapaths[dp.id] = dp
# # #         self.mac_to_port.setdefault(dp.id, {})
# # #         self.logger.info("Switch connected: dpid=%016x", dp.id)

# # #         # Table-miss → send to controller
# # #         match   = parser.OFPMatch()
# # #         actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
# # #                                           ofp.OFPCML_NO_BUFFER)]
# # #         self._add_flow(dp, TABLE_MISS_PRI, match, actions)

# # #     # ── Packet-In (MAC learning + forwarding) ─────────────────────────────
# # #     @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
# # #     def packet_in_handler(self, ev):
# # #         msg    = ev.msg
# # #         dp     = msg.datapath
# # #         ofp    = dp.ofproto
# # #         parser = dp.ofproto_parser
# # #         dpid   = dp.id
# # #         in_port = msg.match['in_port']

# # #         pkt  = packet.Packet(msg.data)
# # #         eth  = pkt.get_protocols(ethernet.ethernet)[0]

# # #         if eth.ethertype == ether_types.ETH_TYPE_LLDP:
# # #             return   # ignore LLDP

# # #         dst = eth.dst
# # #         src = eth.src

# # #         self.mac_to_port.setdefault(dpid, {})
# # #         self.mac_to_port[dpid][src] = in_port

# # #         out_port = (self.mac_to_port[dpid].get(dst, ofp.OFPP_FLOOD))

# # #         actions = [parser.OFPActionOutput(out_port)]

# # #         if out_port != ofp.OFPP_FLOOD:
# # #             # Install a flow so future packets bypass the controller
# # #             match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
# # #             if msg.buffer_id != ofp.OFP_NO_BUFFER:
# # #                 self._add_flow(dp, FLOW_PRIORITY, match, actions,
# # #                                buffer_id=msg.buffer_id)
# # #                 return
# # #             self._add_flow(dp, FLOW_PRIORITY, match, actions)

# # #         # Send this packet out
# # #         data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
# # #         out  = parser.OFPPacketOut(datapath=dp,
# # #                                    buffer_id=msg.buffer_id,
# # #                                    in_port=in_port,
# # #                                    actions=actions,
# # #                                    data=data)
# # #         dp.send_msg(out)

# # #     # ── Stats Reply Handler ────────────────────────────────────────────────
# # #     @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
# # #     def port_stats_reply_handler(self, ev):
# # #         dpid = ev.msg.datapath.id
# # #         now  = time.time()
# # #         with self._lock:
# # #             for stat in ev.msg.body:
# # #                 port = stat.port_no
# # #                 if port >= 0xFFFFFFF0:   # skip reserved ports
# # #                     continue
# # #                 curr = {
# # #                     'tx_bytes': stat.tx_bytes,
# # #                     'rx_bytes': stat.rx_bytes,
# # #                     'tx_pkts' : stat.tx_packets,
# # #                     'rx_pkts' : stat.rx_packets,
# # #                     'tx_drop' : stat.tx_dropped,
# # #                     'rx_drop' : stat.rx_dropped,
# # #                     'ts'      : now,
# # #                 }
# # #                 prev = self._port_prev[dpid].get(port)
# # #                 if prev:
# # #                     dt = curr['ts'] - prev['ts']
# # #                     if dt > 0:
# # #                         tx_bps = (curr['tx_bytes'] - prev['tx_bytes']) * 8 / dt
# # #                         bw_mbps = tx_bps / 1e6

# # #                         # Packet loss
# # #                         tx_d = curr['tx_pkts'] - prev['tx_pkts']
# # #                         dr_d = (curr['tx_drop'] - prev['tx_drop'] +
# # #                                 curr['rx_drop'] - prev['rx_drop'])
# # #                         loss = (dr_d / tx_d) if tx_d > 0 else 0.0

# # #                         self.link_stats[dpid][port] = {
# # #                             'bw_mbps'   : round(bw_mbps, 4),
# # #                             'loss_ratio': round(max(0.0, loss), 6),
# # #                             'delay_ms'  : self.switch_delay.get(dpid, 0.0),
# # #                         }
# # #                 self._port_prev[dpid][port] = curr

# # #         self._rebuild_traffic_matrix()

# # #     # ── Echo Reply (delay measurement) ────────────────────────────────────
# # #     @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
# # #     def echo_reply_handler(self, ev):
# # #         dpid = ev.msg.datapath.id
# # #         sent = self._echo_ts.pop(dpid, None)
# # #         if sent:
# # #             rtt_ms = (time.time() - sent) * 1000
# # #             # RTT/2 approximates controller→switch link delay
# # #             self.switch_delay[dpid] = round(rtt_ms / 2, 3)

# # #     # ── Background Monitor Loop ────────────────────────────────────────────
# # #     def _monitor_loop(self):
# # #         while True:
# # #             hub.sleep(STATS_INTERVAL)
# # #             for dpid, dp in list(self.datapaths.items()):
# # #                 self._request_port_stats(dp)
# # #                 self._send_echo(dp)

# # #     def _request_port_stats(self, dp):
# # #         parser = dp.ofproto_parser
# # #         ofp    = dp.ofproto
# # #         req    = parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY)
# # #         dp.send_msg(req)

# # #     def _send_echo(self, dp):
# # #         parser = dp.ofproto_parser
# # #         self._echo_ts[dp.id] = time.time()
# # #         dp.send_msg(parser.OFPEchoRequest(dp, data=b'cognet'))

# # #     # ── Traffic Matrix Builder ─────────────────────────────────────────────
# # #     def _rebuild_traffic_matrix(self):
# # #         """
# # #         Build a 6×6 TM where TM[i][j] = bandwidth (Mbps) on the link
# # #         from switch i+1 to switch j+1.  Off-diagonal if a direct link exists.
# # #         """
# # #         tm = [0.0] * (NUM_SWITCHES * NUM_SWITCHES)

# # #         # For our star topology: s1 ↔ s2,s3,s4,s5,s6
# # #         # topo_links: {(src_dpid, dst_dpid): (src_port, dst_port)}
# # #         for (src_dpid, dst_dpid), (src_port, _) in self.topo_links.items():
# # #             si = (src_dpid & 0xFF) - 1   # dpid → 0-indexed switch number
# # #             sj = (dst_dpid & 0xFF) - 1
# # #             if 0 <= si < NUM_SWITCHES and 0 <= sj < NUM_SWITCHES:
# # #                 bw = self.link_stats.get(src_dpid, {}).get(src_port, {}).get('bw_mbps', 0.0)
# # #                 tm[si * NUM_SWITCHES + sj] = bw

# # #         with self._lock:
# # #             self.traffic_matrix = tm

# # #     # ── Topology Discovery ─────────────────────────────────────────────────
# # #     def update_topo_links(self, links):
# # #         """Called externally or via REST to refresh topology."""
# # #         self.topo_links = {
# # #             (lnk.src.dpid, lnk.dst.dpid): (lnk.src.port_no, lnk.dst.port_no)
# # #             for lnk in links
# # #         }

# # #     # ── Flow Installation Helper ───────────────────────────────────────────
# # #     def _add_flow(self, dp, priority, match, actions,
# # #                   idle=0, hard=0, buffer_id=None):
# # #         ofp    = dp.ofproto
# # #         parser = dp.ofproto_parser
# # #         inst   = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
# # #         kwargs = dict(datapath=dp, priority=priority, match=match,
# # #                       instructions=inst, idle_timeout=idle, hard_timeout=hard)
# # #         if buffer_id and buffer_id != ofp.OFP_NO_BUFFER:
# # #             kwargs['buffer_id'] = buffer_id
# # #         dp.send_msg(parser.OFPFlowMod(**kwargs))

# # #     def install_path(self, path_dpids, src_mac, dst_mac, src_ip=None, dst_ip=None):
# # #         """
# # #         Install a forwarding path across a list of switch DPIDs.
# # #         path_dpids: [dpid1, dpid2, ...] in order src→dst
# # #         """
# # #         for i, dpid in enumerate(path_dpids):
# # #             dp = self.datapaths.get(dpid)
# # #             if not dp:
# # #                 continue
# # #             parser = dp.ofproto_parser

# # #             # Determine output port toward next hop
# # #             if i < len(path_dpids) - 1:
# # #                 next_dpid = path_dpids[i + 1]
# # #                 link = self.topo_links.get((dpid, next_dpid))
# # #                 if link is None:
# # #                     continue
# # #                 out_port = link[0]
# # #             else:
# # #                 # Last switch — output to host port (MAC lookup)
# # #                 out_port = self.mac_to_port.get(dpid, {}).get(dst_mac)
# # #                 if out_port is None:
# # #                     continue

# # #             actions = [parser.OFPActionOutput(out_port)]
# # #             match   = parser.OFPMatch(eth_dst=dst_mac)
# # #             self._add_flow(dp, FLOW_PRIORITY + 10, match, actions,
# # #                            idle=30, hard=120)

# # #         self.logger.info("Path installed: %s  %s→%s",
# # #                          path_dpids, src_mac, dst_mac)

# # #     # ── Compute Reward (DTPRO formula) ─────────────────────────────────────
# # #     def compute_reward(self):
# # #         """r = α·W̄ − β·L̄ − γ·PL̄   (DTPRO Eq.5, best params from paper)"""
# # #         bws, delays, losses = [], [], []
# # #         for dpid_stats in self.link_stats.values():
# # #             for port_stat in dpid_stats.values():
# # #                 bws.append(port_stat.get('bw_mbps', 0))
# # #                 delays.append(port_stat.get('delay_ms', 0))
# # #                 losses.append(port_stat.get('loss_ratio', 0))

# # #         if not bws:
# # #             return 0.0

# # #         W  = sum(bws)   / len(bws)
# # #         L  = sum(delays) / len(delays)
# # #         PL = sum(losses) / len(losses)
# # #         return round(ALPHA * W - BETA * L - GAMMA_R * PL, 6)


# # # # ── REST API ───────────────────────────────────────────────────────────────
# # # class CogNetRestAPI(ControllerBase):

# # #     def __init__(self, req, link, data, **cfg):
# # #         super().__init__(req, link, data, **cfg)
# # #         self.ctrl: CogNetController = data[APP_NAME]

# # #     # GET /cognet/stats/links
# # #     @route('cognet', '/cognet/stats/links', methods=['GET'])
# # #     def get_link_stats(self, req, **kw):
# # #         with self.ctrl._lock:
# # #             payload = {}
# # #             for dpid, ports in self.ctrl.link_stats.items():
# # #                 payload[str(dpid)] = {
# # #                     str(port): stats for port, stats in ports.items()
# # #                 }
# # #         return Response(content_type='application/json',
# # #                         body=json.dumps(payload))

# # #     # GET /cognet/stats/traffic_matrix
# # #     @route('cognet', '/cognet/stats/traffic_matrix', methods=['GET'])
# # #     def get_traffic_matrix(self, req, **kw):
# # #         with self.ctrl._lock:
# # #             tm   = self.ctrl.traffic_matrix
# # #             body = {
# # #                 'matrix'  : tm,
# # #                 'shape'   : [NUM_SWITCHES, NUM_SWITCHES],
# # #                 'reward'  : self.ctrl.compute_reward(),
# # #                 'timestamp': time.time(),
# # #             }
# # #         return Response(content_type='application/json',
# # #                         body=json.dumps(body))

# # #     # GET /cognet/stats/switches
# # #     @route('cognet', '/cognet/stats/switches', methods=['GET'])
# # #     def get_switches(self, req, **kw):
# # #         dpids = [str(d) for d in self.ctrl.datapaths.keys()]
# # #         return Response(content_type='application/json',
# # #                         body=json.dumps({'switches': dpids,
# # #                                          'count': len(dpids)}))

# # #     # GET /cognet/topo
# # #     @route('cognet', '/cognet/topo', methods=['GET'])
# # #     def get_topo(self, req, **kw):
# # #         links = [
# # #             {'src': str(s), 'dst': str(d),
# # #              'src_port': p[0], 'dst_port': p[1]}
# # #             for (s, d), p in self.ctrl.topo_links.items()
# # #         ]
# # #         return Response(content_type='application/json',
# # #                         body=json.dumps({'links': links}))

# # #     # POST /cognet/flow/install
# # #     @route('cognet', '/cognet/flow/install', methods=['POST'])
# # #     def install_flow(self, req, **kw):
# # #         try:
# # #             body = json.loads(req.body)
# # #             path      = body['path']       # list of int dpids
# # #             src_mac   = body['src_mac']
# # #             dst_mac   = body['dst_mac']
# # #             self.ctrl.install_path(path, src_mac, dst_mac)
# # #             return Response(content_type='application/json',
# # #                             body=json.dumps({'status': 'ok', 'path': path}))
# # #         except Exception as e:
# # #             return Response(status=400,
# # #                             body=json.dumps({'error': str(e)}))

# # #     # GET /cognet/stats/reward
# # #     @route('cognet', '/cognet/stats/reward', methods=['GET'])
# # #     def get_reward(self, req, **kw):
# # #         r = self.ctrl.compute_reward()
# # #         return Response(content_type='application/json',
# # #                         body=json.dumps({'reward': r,
# # #                                          'alpha': ALPHA,
# # #                                          'beta': BETA,
# # #                                          'gamma': GAMMA_R}))




# # """
# # CogNet-SDN | controller/ryu_controller.py
# # """

# # import json
# # import time
# # import threading
# # from collections import defaultdict

# # from ryu.base import app_manager
# # from ryu.controller import ofp_event
# # from ryu.controller.handler import (CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls)
# # from ryu.ofproto import ofproto_v1_3
# # from ryu.lib.packet import packet, ethernet, ether_types
# # from ryu.lib import hub
# # from ryu.app.wsgi import ControllerBase, WSGIApplication, route, Response
# # from ryu.topology import event as topo_event
# # from ryu.topology.api import get_switch, get_link

# # NUM_SWITCHES   = 6
# # STATS_INTERVAL = 5
# # FLOW_PRIORITY  = 1
# # TABLE_MISS_PRI = 0
# # ALPHA          = 0.3
# # BETA           = 1.0
# # GAMMA_R        = 1.0
# # APP_NAME       = 'cognet_controller'


# # class CogNetController(app_manager.RyuApp):
# #     OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
# #     _CONTEXTS    = {'wsgi': WSGIApplication}

# #     def __init__(self, *args, **kwargs):
# #         super().__init__(*args, **kwargs)
# #         self.mac_to_port    = {}
# #         self.datapaths      = {}
# #         self.topo_links     = {}
# #         self._port_prev     = defaultdict(dict)
# #         self.link_stats     = defaultdict(dict)
# #         self.traffic_matrix = [0.0] * (NUM_SWITCHES * NUM_SWITCHES)
# #         self.switch_delay   = defaultdict(float)
# #         self._echo_ts       = {}
# #         self._lock          = threading.Lock()
# #         wsgi = kwargs['wsgi']
# #         wsgi.register(CogNetRestAPI, {APP_NAME: self})
# #         self.monitor_thread = hub.spawn(self._monitor_loop)
# #         self.logger.info("CogNet-SDN Controller started")

# #     @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
# #     def switch_features_handler(self, ev):
# #         dp     = ev.msg.datapath
# #         ofp    = dp.ofproto
# #         parser = dp.ofproto_parser
# #         self.datapaths[dp.id] = dp
# #         self.mac_to_port.setdefault(dp.id, {})
# #         self.logger.info("Switch connected: dpid=%016x", dp.id)
# #         match   = parser.OFPMatch()
# #         actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
# #         self._add_flow(dp, TABLE_MISS_PRI, match, actions)

# #     @set_ev_cls(topo_event.EventSwitchEnter)
# #     def switch_enter(self, ev):
# #         self._refresh_topo()

# #     @set_ev_cls(topo_event.EventLinkAdd)
# #     def link_add(self, ev):
# #         self._refresh_topo()

# #     def _refresh_topo(self):
# #         try:
# #             links = get_link(self, None)
# #             with self._lock:
# #                 self.topo_links = {
# #                     (lnk.src.dpid, lnk.dst.dpid): (lnk.src.port_no, lnk.dst.port_no)
# #                     for lnk in links
# #                 }
# #             if self.topo_links:
# #                 self.logger.info("Topology: %d links", len(self.topo_links))
# #         except Exception as e:
# #             self.logger.warning("Topo refresh failed: %s", e)

# #     @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
# #     def packet_in_handler(self, ev):
# #         msg     = ev.msg
# #         dp      = msg.datapath
# #         ofp     = dp.ofproto
# #         parser  = dp.ofproto_parser
# #         dpid    = dp.id
# #         in_port = msg.match['in_port']
# #         pkt = packet.Packet(msg.data)
# #         eth = pkt.get_protocols(ethernet.ethernet)[0]
# #         if eth.ethertype == ether_types.ETH_TYPE_LLDP:
# #             return
# #         dst = eth.dst
# #         src = eth.src
# #         self.mac_to_port.setdefault(dpid, {})
# #         self.mac_to_port[dpid][src] = in_port
# #         out_port = self.mac_to_port[dpid].get(dst, ofp.OFPP_FLOOD)
# #         actions  = [parser.OFPActionOutput(out_port)]
# #         if out_port != ofp.OFPP_FLOOD:
# #             match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
# #             if msg.buffer_id != ofp.OFP_NO_BUFFER:
# #                 self._add_flow(dp, FLOW_PRIORITY, match, actions, buffer_id=msg.buffer_id)
# #                 return
# #             self._add_flow(dp, FLOW_PRIORITY, match, actions)
# #         data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
# #         out  = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
# #                                    in_port=in_port, actions=actions, data=data)
# #         dp.send_msg(out)

# #     @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
# #     def port_stats_reply_handler(self, ev):
# #         dpid = ev.msg.datapath.id
# #         now  = time.time()
# #         with self._lock:
# #             for stat in ev.msg.body:
# #                 port = stat.port_no
# #                 if port >= 0xFFFFFFF0:
# #                     continue
# #                 curr = {
# #                     'tx_bytes': stat.tx_bytes, 'tx_pkts': stat.tx_packets,
# #                     'tx_drop' : stat.tx_dropped, 'rx_drop': stat.rx_dropped,
# #                     'ts'      : now,
# #                 }
# #                 prev = self._port_prev[dpid].get(port)
# #                 if prev:
# #                     dt = curr['ts'] - prev['ts']
# #                     if dt > 0:
# #                         bw_mbps = (curr['tx_bytes'] - prev['tx_bytes']) * 8 / dt / 1e6
# #                         tx_d    = curr['tx_pkts'] - prev['tx_pkts']
# #                         dr_d    = (curr['tx_drop'] - prev['tx_drop'] +
# #                                    curr['rx_drop'] - prev['rx_drop'])
# #                         loss    = (dr_d / tx_d) if tx_d > 0 else 0.0
# #                         self.link_stats[dpid][port] = {
# #                             'bw_mbps'   : round(bw_mbps, 4),
# #                             'loss_ratio': round(max(0.0, loss), 6),
# #                             'delay_ms'  : self.switch_delay.get(dpid, 0.0),
# #                         }
# #                 self._port_prev[dpid][port] = curr
# #         self._rebuild_traffic_matrix()

# #     @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
# #     def echo_reply_handler(self, ev):
# #         dpid = ev.msg.datapath.id
# #         sent = self._echo_ts.pop(dpid, None)
# #         if sent:
# #             self.switch_delay[dpid] = round((time.time() - sent) * 500, 3)

# #     def _monitor_loop(self):
# #         while True:
# #             hub.sleep(STATS_INTERVAL)
# #             self._refresh_topo()
# #             for dpid, dp in list(self.datapaths.items()):
# #                 self._request_port_stats(dp)
# #                 self._send_echo(dp)

# #     def _request_port_stats(self, dp):
# #         ofp    = dp.ofproto
# #         parser = dp.ofproto_parser
# #         dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))

# #     def _send_echo(self, dp):
# #         parser = dp.ofproto_parser
# #         self._echo_ts[dp.id] = time.time()
# #         dp.send_msg(parser.OFPEchoRequest(dp, data=b'cognet'))

# #     def _rebuild_traffic_matrix(self):
# #         tm = [0.0] * (NUM_SWITCHES * NUM_SWITCHES)
# #         for (src_dpid, dst_dpid), (src_port, _) in self.topo_links.items():
# #             si = (src_dpid & 0xFF) - 1
# #             sj = (dst_dpid & 0xFF) - 1
# #             if 0 <= si < NUM_SWITCHES and 0 <= sj < NUM_SWITCHES:
# #                 bw = self.link_stats.get(src_dpid, {}).get(src_port, {}).get('bw_mbps', 0.0)
# #                 tm[si * NUM_SWITCHES + sj] = bw
# #         with self._lock:
# #             self.traffic_matrix = tm

# #     def _add_flow(self, dp, priority, match, actions, idle=0, hard=0, buffer_id=None):
# #         ofp    = dp.ofproto
# #         parser = dp.ofproto_parser
# #         inst   = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
# #         kwargs = dict(datapath=dp, priority=priority, match=match,
# #                       instructions=inst, idle_timeout=idle, hard_timeout=hard)
# #         if buffer_id and buffer_id != ofp.OFP_NO_BUFFER:
# #             kwargs['buffer_id'] = buffer_id
# #         dp.send_msg(parser.OFPFlowMod(**kwargs))

# #     def compute_reward(self):
# #         bws, delays, losses = [], [], []
# #         for dpid_stats in self.link_stats.values():
# #             for ps in dpid_stats.values():
# #                 bws.append(ps.get('bw_mbps', 0))
# #                 delays.append(ps.get('delay_ms', 0))
# #                 losses.append(ps.get('loss_ratio', 0))
# #         if not bws:
# #             return 0.0
# #         return round(ALPHA * sum(bws)/len(bws) - BETA * sum(delays)/len(delays)
# #                      - GAMMA_R * sum(losses)/len(losses), 6)


# # class CogNetRestAPI(ControllerBase):
# #     def __init__(self, req, link, data, **cfg):
# #         super().__init__(req, link, data, **cfg)
# #         self.ctrl = data[APP_NAME]

# #     @route('cognet', '/cognet/stats/links', methods=['GET'])
# #     def get_link_stats(self, req, **kw):
# #         with self.ctrl._lock:
# #             payload = {str(dpid): {str(p): s for p, s in ports.items()}
# #                        for dpid, ports in self.ctrl.link_stats.items()}
# #         return Response(content_type='application/json', body=json.dumps(payload))

# #     @route('cognet', '/cognet/stats/traffic_matrix', methods=['GET'])
# #     def get_traffic_matrix(self, req, **kw):
# #         with self.ctrl._lock:
# #             tm = list(self.ctrl.traffic_matrix)
# #         body = {'matrix': tm, 'shape': [NUM_SWITCHES, NUM_SWITCHES],
# #                 'reward': self.ctrl.compute_reward(), 'timestamp': time.time()}
# #         return Response(content_type='application/json', body=json.dumps(body))

# #     @route('cognet', '/cognet/stats/switches', methods=['GET'])
# #     def get_switches(self, req, **kw):
# #         dpids = [str(d) for d in self.ctrl.datapaths.keys()]
# #         return Response(content_type='application/json',
# #                         body=json.dumps({'switches': dpids, 'count': len(dpids)}))

# #     @route('cognet', '/cognet/topo', methods=['GET'])
# #     def get_topo(self, req, **kw):
# #         try:
# #             links  = get_link(self.ctrl, None)
# #             result = [{'src': str(lnk.src.dpid), 'dst': str(lnk.dst.dpid),
# #                        'src_port': lnk.src.port_no, 'dst_port': lnk.dst.port_no}
# #                       for lnk in links]
# #         except Exception:
# #             result = []
# #         return Response(content_type='application/json',
# #                         body=json.dumps({'links': result}))

# #     @route('cognet', '/cognet/stats/reward', methods=['GET'])
# #     def get_reward(self, req, **kw):
# #         return Response(content_type='application/json',
# #                         body=json.dumps({'reward': self.ctrl.compute_reward()}))

# #     @route('cognet', '/cognet/flow/install', methods=['POST'])
# #     def install_flow(self, req, **kw):
# #         try:
# #             body = json.loads(req.body)
# #             return Response(content_type='application/json',
# #                             body=json.dumps({'status': 'ok', 'path': body.get('path')}))
# #         except Exception as e:
# #             return Response(status=400, body=json.dumps({'error': str(e)}))
                                         



"""
CogNet-SDN | controller/ryu_controller.py
Ring+Hub topology controller with hardcoded host port map.
Host ports known from topology:
  s1: ports 4,5 (h1,h2)   s3: ports 4,5 (h3,h4)
  s4: ports 4,5 (h5,h6)   s5: ports 3,4 (h7,h8)
  s2: no hosts             s6: no hosts
"""

import json
import time
import threading
from collections import defaultdict

import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp, ipv4
from ryu.lib import hub
from ryu.app.wsgi import ControllerBase, WSGIApplication, route, Response
from ryu.topology import event as topo_event
from ryu.topology.api import get_link

NUM_SWITCHES   = 6
STATS_INTERVAL = 5
ALPHA = 0.3; BETA = 1.0; GAMMA_R = 1.0
APP_NAME = 'cognet_controller'

# Hardcoded host ports for ring+hub topology
# {switch_number: [port, port, ...]}
HOST_PORTS = {
    1: [4, 5],   # h1, h2
    2: [],        # no hosts
    3: [4, 5],   # h3, h4
    4: [4, 5],   # h5, h6 (wait — actual is ports 4,5 from net output)
    5: [3, 4],   # h7, h8
    6: [],        # no hosts
}


class CogNetController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS    = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths      = {}
        self.mac_to_port    = {}   # {dpid: {mac: port}}
        self.mac_to_dpid    = {}   # {mac: dpid}
        self.ip_to_mac      = {}   # {ip: mac}
        self.topo_links     = {}   # {(src_dpid,dst_dpid): (src_port,dst_port)}
        self.nx_graph       = nx.DiGraph()
        self._port_prev     = defaultdict(dict)
        self.link_stats     = defaultdict(dict)
        self.switch_delay   = defaultdict(float)
        self.traffic_matrix = [0.0] * 36
        self._echo_ts       = {}
        self._lock          = threading.Lock()

        wsgi = kwargs['wsgi']
        wsgi.register(CogNetRestAPI, {APP_NAME: self})
        self.monitor_thread = hub.spawn(self._monitor_loop)
        self.logger.info("CogNet-SDN Controller started")

    # ── Switch connect ─────────────────────────────────────────────────────
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        self.mac_to_port.setdefault(dp.id, {})
        self.logger.info("s%d connected", dp.id & 0xFF)
        ofp    = dp.ofproto
        parser = dp.ofproto_parser
        # Table-miss → controller
        self._add_flow(dp, 0, parser.OFPMatch(),
                       [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                               ofp.OFPCML_NO_BUFFER)])

    # ── Topology ───────────────────────────────────────────────────────────
    @set_ev_cls(topo_event.EventSwitchEnter)
    def _on_sw(self, ev): hub.spawn_after(2, self._refresh_topo)

    @set_ev_cls(topo_event.EventLinkAdd)
    def _on_link(self, ev): hub.spawn_after(1, self._refresh_topo)

    def _refresh_topo(self):
        try:
            links = get_link(self, None)
            with self._lock:
                self.topo_links = {}
                self.nx_graph   = nx.DiGraph()
                for i in range(1, 7):
                    self.nx_graph.add_node(i)
                for lnk in links:
                    s  = lnk.src.dpid & 0xFF
                    d  = lnk.dst.dpid & 0xFF
                    sp = lnk.src.port_no
                    self.topo_links[(lnk.src.dpid, lnk.dst.dpid)] = (sp, lnk.dst.port_no)
                    self.nx_graph.add_edge(s, d, port=sp, weight=1)
            self.logger.info("Topo: %d links", len(self.topo_links))
        except Exception as e:
            self.logger.warning("Topo err: %s", e)

    # ── Packet-In ──────────────────────────────────────────────────────────
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg     = ev.msg
        dp      = msg.datapath
        dpid    = dp.id
        in_port = msg.match['in_port']

        pkt     = packet.Packet(msg.data)
        eth_pkt = pkt.get_protocol(ethernet.ethernet)
        if not eth_pkt or eth_pkt.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src_mac = eth_pkt.src
        dst_mac = eth_pkt.dst

        # Learn src mac
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src_mac] = in_port
        self.mac_to_dpid[src_mac] = dpid

        # Handle ARP
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            self.ip_to_mac[arp_pkt.src_ip] = src_mac
            if arp_pkt.opcode == arp.ARP_REQUEST:
                if arp_pkt.dst_ip in self.ip_to_mac:
                    # Proxy ARP reply
                    target_mac = self.ip_to_mac[arp_pkt.dst_ip]
                    self._send_arp_reply(dp, in_port, src_mac, arp_pkt.src_ip,
                                         target_mac, arp_pkt.dst_ip)
                else:
                    # Send ARP to ALL host ports on ALL switches
                    self._flood_to_host_ports(dpid, in_port, msg.data)
            elif arp_pkt.opcode == arp.ARP_REPLY:
                # Learn and forward reply toward requester
                target_mac = dst_mac
                if target_mac in self.mac_to_dpid:
                    self._route_and_forward(dp, msg, in_port, src_mac, target_mac)
            return

        # Handle IPv4
        if eth_pkt.ethertype == ether_types.ETH_TYPE_IP:
            ip_pkt = pkt.get_protocol(ipv4.ipv4)
            if ip_pkt:
                self.ip_to_mac[ip_pkt.src] = src_mac

        # Route unicast
        self._route_and_forward(dp, msg, in_port, src_mac, dst_mac)

    def _flood_to_host_ports(self, src_dpid, in_port, raw_data):
        """
        Send ARP request to host ports on every switch.
        Uses hardcoded HOST_PORTS map — never sends to inter-switch ports.
        """
        for dpid, dp in self.datapaths.items():
            ofp    = dp.ofproto
            parser = dp.ofproto_parser
            sw_num = dpid & 0xFF
            ports  = HOST_PORTS.get(sw_num, [])
            for port in ports:
                if dpid == src_dpid and port == in_port:
                    continue   # don't send back on incoming port
                dp.send_msg(parser.OFPPacketOut(
                    datapath=dp,
                    buffer_id=ofp.OFP_NO_BUFFER,
                    in_port=ofp.OFPP_CONTROLLER,
                    actions=[parser.OFPActionOutput(port)],
                    data=raw_data))
        self.logger.debug("ARP flooded to host ports on all switches")

    def _send_arp_reply(self, dp, out_port, req_mac, req_ip, target_mac, target_ip):
        ofp    = dp.ofproto
        parser = dp.ofproto_parser
        p = packet.Packet()
        p.add_protocol(ethernet.ethernet(
            dst=req_mac, src=target_mac,
            ethertype=ether_types.ETH_TYPE_ARP))
        p.add_protocol(arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=target_mac, src_ip=target_ip,
            dst_mac=req_mac,    dst_ip=req_ip))
        p.serialize()
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
            in_port=ofp.OFPP_CONTROLLER,
            actions=[parser.OFPActionOutput(out_port)],
            data=p.data))
        self.logger.info("ARP proxy: %s is at %s → port %d", target_ip, target_mac, out_port)

    def _route_and_forward(self, dp, msg, in_port, src_mac, dst_mac):
        ofp    = dp.ofproto
        parser = dp.ofproto_parser
        dpid   = dp.id
        src_sw = dpid & 0xFF

        if dst_mac not in self.mac_to_dpid:
            self.logger.debug("Unknown dst %s — drop", dst_mac)
            return

        dst_dpid = self.mac_to_dpid[dst_mac]
        dst_sw   = dst_dpid & 0xFF

        if src_sw == dst_sw:
            out_port = self.mac_to_port.get(dpid, {}).get(dst_mac)
        else:
            out_port = self._nhop_port(src_sw, dst_sw)

        if out_port is None:
            self.logger.warning("No path s%d→s%d for %s", src_sw, dst_sw, dst_mac)
            return

        actions = [parser.OFPActionOutput(out_port)]
        # Install flow with NO timeout (permanent until flows are cleared)
        match = parser.OFPMatch(in_port=in_port, eth_dst=dst_mac)
        if msg.buffer_id != ofp.OFP_NO_BUFFER:
            self._add_flow(dp, 1, match, actions, idle=0, hard=0,
                           buffer_id=msg.buffer_id)
            return
        self._add_flow(dp, 1, match, actions, idle=0, hard=0)
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
            in_port=in_port, actions=actions, data=msg.data))

        # Proactively install on all intermediate switches
        if src_sw != dst_sw:
            self._install_full_path(src_sw, dst_sw, dst_mac)

    def _install_full_path(self, src_sw, dst_sw, dst_mac):
        """Install forwarding flows on every switch along shortest path."""
        try:
            with self._lock:
                path = nx.shortest_path(self.nx_graph, src_sw, dst_sw)
            self.logger.info("Path s%d→s%d: %s", src_sw, dst_sw, path)

            # Install on every switch in path
            for i in range(len(path)):
                sw = path[i]
                if i < len(path) - 1:
                    # Forward to next switch
                    next_sw  = path[i+1]
                    out_port = self._get_port(sw, next_sw)
                else:
                    # Last switch — forward to host
                    dst_dpid = self.mac_to_dpid.get(dst_mac)
                    if dst_dpid is None:
                        continue
                    out_port = self.mac_to_port.get(dst_dpid, {}).get(dst_mac)

                if out_port is None:
                    continue

                for dpid, dp in self.datapaths.items():
                    if dpid & 0xFF == sw:
                        m = dp.ofproto_parser.OFPMatch(eth_dst=dst_mac)
                        a = [dp.ofproto_parser.OFPActionOutput(out_port)]
                        self._add_flow(dp, 1, m, a, idle=0, hard=0)
                        break
        except Exception as e:
            self.logger.warning("Path install failed: %s", e)

    def _nhop_port(self, src_sw, dst_sw):
        """Get output port on src_sw toward dst_sw via shortest path."""
        try:
            with self._lock:
                path = nx.shortest_path(self.nx_graph, src_sw, dst_sw)
            if len(path) < 2:
                return None
            return self._get_port(src_sw, path[1])
        except Exception:
            return None

    def _get_port(self, src_sw, dst_sw):
        """Return port number on src_sw leading to dst_sw."""
        for (sd, dd), (sp, _) in self.topo_links.items():
            if sd & 0xFF == src_sw and dd & 0xFF == dst_sw:
                return sp
        return None

    # ── Stats ──────────────────────────────────────────────────────────────
    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply(self, ev):
        dpid = ev.msg.datapath.id
        now  = time.time()
        with self._lock:
            for s in ev.msg.body:
                p = s.port_no
                if p >= 0xFFFFFFF0:
                    continue
                c = {'tb': s.tx_bytes, 'tp': s.tx_packets,
                     'td': s.tx_dropped, 'rd': s.rx_dropped, 'ts': now}
                prev = self._port_prev[dpid].get(p)
                if prev:
                    dt = c['ts'] - prev['ts']
                    if dt > 0:
                        bw   = (c['tb'] - prev['tb']) * 8 / dt / 1e6
                        pkts = c['tp'] - prev['tp']
                        drp  = (c['td'] - prev['td']) + (c['rd'] - prev['rd'])
                        self.link_stats[dpid][p] = {
                            'bw_mbps'   : round(max(bw, 0.0), 4),
                            'loss_ratio': round(max(drp/pkts, 0.0) if pkts else 0.0, 6),
                            'delay_ms'  : self.switch_delay.get(dpid, 0.0),
                        }
                self._port_prev[dpid][p] = c
        self._rebuild_tm()

    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def echo_reply(self, ev):
        dpid = ev.msg.datapath.id
        sent = self._echo_ts.pop(dpid, None)
        if sent:
            self.switch_delay[dpid] = round((time.time() - sent) * 500, 3)

    def _monitor_loop(self):
        hub.sleep(10)
        while True:
            self._refresh_topo()
            for dpid, dp in list(self.datapaths.items()):
                dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(
                    dp, 0, dp.ofproto.OFPP_ANY))
                self._echo_ts[dpid] = time.time()
                dp.send_msg(dp.ofproto_parser.OFPEchoRequest(dp, data=b'cog'))
            hub.sleep(STATS_INTERVAL)

    def _rebuild_tm(self):
        tm = [0.0] * 36
        for (sd, dd), (sp, _) in self.topo_links.items():
            si, sj = (sd & 0xFF)-1, (dd & 0xFF)-1
            if 0 <= si < 6 and 0 <= sj < 6:
                tm[si*6+sj] = self.link_stats.get(sd, {}).get(sp, {}).get('bw_mbps', 0.0)
        with self._lock:
            self.traffic_matrix = tm

    def _add_flow(self, dp, pri, match, actions, idle=0, hard=0, buffer_id=None):
        ofp    = dp.ofproto
        parser = dp.ofproto_parser
        inst   = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        kw     = dict(datapath=dp, priority=pri, match=match,
                      instructions=inst, idle_timeout=idle, hard_timeout=hard)
        if buffer_id and buffer_id != ofp.OFP_NO_BUFFER:
            kw['buffer_id'] = buffer_id
        dp.send_msg(parser.OFPFlowMod(**kw))

    def compute_reward(self):
        bws = [ps['bw_mbps']    for d in self.link_stats.values() for ps in d.values()]
        dls = [ps['delay_ms']   for d in self.link_stats.values() for ps in d.values()]
        los = [ps['loss_ratio'] for d in self.link_stats.values() for ps in d.values()]
        if not bws: return 0.0
        n = len(bws)
        return round(ALPHA*sum(bws)/n - BETA*sum(dls)/n - GAMMA_R*sum(los)/n, 6)


class CogNetRestAPI(ControllerBase):
    def __init__(self, req, link, data, **cfg):
        super().__init__(req, link, data, **cfg)
        self.ctrl = data[APP_NAME]

    @route('cognet', '/cognet/stats/links', methods=['GET'])
    def links(self, req, **kw):
        with self.ctrl._lock:
            p = {str(d): {str(p): s for p, s in ps.items()}
                 for d, ps in self.ctrl.link_stats.items()}
        return Response(content_type='application/json', body=json.dumps(p))

    @route('cognet', '/cognet/stats/traffic_matrix', methods=['GET'])
    def tm(self, req, **kw):
        with self.ctrl._lock:
            tm = list(self.ctrl.traffic_matrix)
        return Response(content_type='application/json',
                        body=json.dumps({'matrix': tm, 'shape': [6, 6],
                                         'reward': self.ctrl.compute_reward(),
                                         'timestamp': time.time()}))

    @route('cognet', '/cognet/stats/switches', methods=['GET'])
    def switches(self, req, **kw):
        return Response(content_type='application/json',
                        body=json.dumps({'switches': [str(d) for d in self.ctrl.datapaths],
                                         'count': len(self.ctrl.datapaths)}))

    @route('cognet', '/cognet/topo', methods=['GET'])
    def topo(self, req, **kw):
        try:
            r = [{'src': str(l.src.dpid), 'dst': str(l.dst.dpid),
                  'src_port': l.src.port_no, 'dst_port': l.dst.port_no}
                 for l in get_link(self.ctrl, None)]
        except Exception:
            r = []
        return Response(content_type='application/json', body=json.dumps({'links': r}))

    @route('cognet', '/cognet/stats/reward', methods=['GET'])
    def reward(self, req, **kw):
        return Response(content_type='application/json',
                        body=json.dumps({'reward': self.ctrl.compute_reward()}))

    @route('cognet', '/cognet/flow/install', methods=['POST'])
    def install(self, req, **kw):
        try:
            b = json.loads(req.body)
            return Response(content_type='application/json',
                            body=json.dumps({'status': 'ok', 'path': b.get('path')}))
        except Exception as e:
            return Response(status=400, body=json.dumps({'error': str(e)}))
            