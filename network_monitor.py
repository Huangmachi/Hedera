# Copyright (C) 2016 Li Cheng at Beijing University of Posts
# and Telecommunications. www.muzixing.com
# Copyright (C) 2016 Huang MaChi at Chongqing University
# of Posts and Telecommunications, China.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import division
import copy
from operator import attrgetter

from ryu import cfg
from ryu.base import app_manager
from ryu.base.app_manager import lookup_service_brick
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub

import setting
from DemandEstimation import demand_estimation


CONF = cfg.CONF


class NetworkMonitor(app_manager.RyuApp):
	"""
		NetworkMonitor is a Ryu app for collecting traffic information.
	"""
	OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

	def __init__(self, *args, **kwargs):
		super(NetworkMonitor, self).__init__(*args, **kwargs)
		self.name = 'monitor'
		self.awareness = lookup_service_brick('awareness')
		self.datapaths = {}
		self.port_stats = {}
		self.port_speed = {}
		self.flow_stats = {}
		self.flow_speed = {}
		self.stats = {}
		self.port_features = {}
		self.free_bandwidth = {}   # {dpid:{port_no:free_bw,},} Unit:Kbit/s
		self.graph = None
		self.capabilities = None
		self.best_paths = None
		# Create four data structures for Hedera specially.
		self.hostsList = []
		self.flows = []   # Record flows that need to be rescheduled. (hmc)
		self.statRecord = []
		self.pre_GFF_path = {}   # Record the last GFF path of flows

		# Start to green thread to monitor traffic and calculating
		# free bandwidth of links respectively.
		self.monitor_thread = hub.spawn(self._monitor)
		self.save_freebandwidth_thread = hub.spawn(self._save_bw_graph)

	def _monitor(self):
		"""
			Main entry method of monitoring traffic.
		"""
		while CONF.weight == 'bw' or CONF.weight == 'hop':
			# Refresh data.
			self.stats['flow'] = {}
			self.stats['port'] = {}
			self.capabilities = None
			self.best_paths = None
			self.statRecord = []
			self.flows = []
			for dp in self.datapaths.values():
				self.port_features.setdefault(dp.id, {})
				self._request_stats(dp)
			hub.sleep(setting.MONITOR_PERIOD)
			if self.stats['flow'] or self.stats['port']:
				self.show_stat('flow')
				self.show_stat('port')
				hub.sleep(1)

	def _save_bw_graph(self):
		"""
			Save bandwidth data into networkx graph object.
		"""
		while CONF.weight == 'bw' or CONF.weight == 'hop':
			self.graph = self.create_bw_graph(self.free_bandwidth)
			self.logger.debug("save free bandwidth")
			hub.sleep(setting.MONITOR_PERIOD)

	@set_ev_cls(ofp_event.EventOFPStateChange,
				[MAIN_DISPATCHER, DEAD_DISPATCHER])
	def _state_change_handler(self, ev):
		"""
			Record datapath information.
		"""
		datapath = ev.datapath
		if ev.state == MAIN_DISPATCHER:
			if not datapath.id in self.datapaths:
				self.logger.debug('register datapath: %016x', datapath.id)
				self.datapaths[datapath.id] = datapath
		elif ev.state == DEAD_DISPATCHER:
			if datapath.id in self.datapaths:
				self.logger.debug('unregister datapath: %016x', datapath.id)
				del self.datapaths[datapath.id]
		else:
			pass


	@set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
	def _port_stats_reply_handler(self, ev):
		"""
			Save port's stats information into self.port_stats.
			Calculate port speed and Save it.
			self.port_stats = {(dpid, port_no):[(tx_bytes, rx_bytes, rx_errors, duration_sec,  duration_nsec),],}
			self.port_speed = {(dpid, port_no):[speed,],}
			Note: Since the transmit performance and receive performance are
			 independent of a port, we calculate the current load of a port only
			 using tx_bytes while finding routing path.
		"""
		body = ev.msg.body
		dpid = ev.msg.datapath.id
		self.stats['port'][dpid] = body
		self.free_bandwidth.setdefault(dpid, {})
		for stat in sorted(body, key=attrgetter('port_no')):
			port_no = stat.port_no
			if port_no != ofproto_v1_3.OFPP_LOCAL:
				key = (dpid, port_no)
				value = (stat.tx_bytes, stat.rx_bytes, stat.rx_errors,
						 stat.duration_sec, stat.duration_nsec)
				self._save_stats(self.port_stats, key, value, 5)

				# Get port speed and Save it.
				pre = 0
				period = setting.MONITOR_PERIOD
				tmp = self.port_stats[key]
				if len(tmp) > 1:
					# Calculate only the tx_bytes, not the rx_bytes. (hmc)
					pre = tmp[-2][0]
					period = self._get_period(tmp[-1][3], tmp[-1][4], tmp[-2][3], tmp[-2][4])
				speed = self._get_speed(self.port_stats[key][-1][0], pre, period)
				self._save_stats(self.port_speed, key, speed, 5)
				self._save_freebandwidth(dpid, port_no, speed)

	@set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
	def port_desc_stats_reply_handler(self, ev):
		"""
			Save port description info.
		"""
		msg = ev.msg
		dpid = msg.datapath.id
		ofproto = msg.datapath.ofproto

		config_dict = {ofproto.OFPPC_PORT_DOWN: "Down",
					   ofproto.OFPPC_NO_RECV: "No Recv",
					   ofproto.OFPPC_NO_FWD: "No Farward",
					   ofproto.OFPPC_NO_PACKET_IN: "No Packet-in"}

		state_dict = {ofproto.OFPPS_LINK_DOWN: "Down",
					  ofproto.OFPPS_BLOCKED: "Blocked",
					  ofproto.OFPPS_LIVE: "Live"}

		ports = []
		for p in ev.msg.body:
			ports.append('port_no=%d hw_addr=%s name=%s config=0x%08x '
						 'state=0x%08x curr=0x%08x advertised=0x%08x '
						 'supported=0x%08x peer=0x%08x curr_speed=%d '
						 'max_speed=%d' %
						 (p.port_no, p.hw_addr,
						  p.name, p.config,
						  p.state, p.curr, p.advertised,
						  p.supported, p.peer, p.curr_speed,
						  p.max_speed))

			if p.config in config_dict:
				config = config_dict[p.config]
			else:
				config = "up"

			if p.state in state_dict:
				state = state_dict[p.state]
			else:
				state = "up"

			# Recording data.
			port_feature = (config, state, p.curr_speed)
			self.port_features[dpid][p.port_no] = port_feature

	@set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
	def _port_status_handler(self, ev):
		"""
			Handle the port status changed event.
		"""
		msg = ev.msg
		ofproto = msg.datapath.ofproto
		reason = msg.reason
		dpid = msg.datapath.id
		port_no = msg.desc.port_no

		reason_dict = {ofproto.OFPPR_ADD: "added",
					   ofproto.OFPPR_DELETE: "deleted",
					   ofproto.OFPPR_MODIFY: "modified", }

		if reason in reason_dict:
			print "switch%d: port %s %s" % (dpid, reason_dict[reason], port_no)
		else:
			print "switch%d: Illeagal port state %s %s" % (dpid, port_no, reason)

	@set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
	def _flow_stats_reply_handler(self, ev):
		"""
			Save flow stats reply information into self.flow_stats.
			Calculate flow speed and Save it.
			(old) self.flow_stats = {dpid:{(in_port, ipv4_dst, out-port):[(packet_count, byte_count, duration_sec,  duration_nsec),],},}
			(old) self.flow_speed = {dpid:{(in_port, ipv4_dst, out-port):[speed,],},}
			(new) self.flow_stats = {dpid:{(priority, ipv4_src, ipv4_dst):[(packet_count, byte_count, duration_sec,  duration_nsec),],},}
			(new) self.flow_speed = {dpid:{(priority, ipv4_src, ipv4_dst):[speed,],},}
			Because the proactive flow entrys don't have 'in_port' and 'out-port' field.
			Note: table-miss, LLDP and ARP flow entries are not what we need, just filter them.
		"""
		body = ev.msg.body
		dpid = ev.msg.datapath.id
		self.statRecord.append(dpid)
		self.stats['flow'][dpid] = body
		self.flow_stats.setdefault(dpid, {})
		self.flow_speed.setdefault(dpid, {})
		for stat in sorted([flow for flow in body if ((flow.priority not in [0, 65535]) and (flow.match.get('ipv4_src')) and (flow.match.get('ipv4_dst')))],
						   key=lambda flow: (flow.priority, flow.match.get('ipv4_src'), flow.match.get('ipv4_dst'))):
			key = (stat.priority, stat.match.get('ipv4_src'), stat.match.get('ipv4_dst'))
			value = (stat.packet_count, stat.byte_count,
					 stat.duration_sec, stat.duration_nsec)
			self._save_stats(self.flow_stats[dpid], key, value, 5)

			# Get flow's speed and Save it.
			pre = 0
			period = setting.MONITOR_PERIOD
			tmp = self.flow_stats[dpid][key]
			if len(tmp) > 1:
				pre = tmp[-2][1]
				period = self._get_period(tmp[-1][2], tmp[-1][3], tmp[-2][2], tmp[-2][3])
			speed = self._get_speed(self.flow_stats[dpid][key][-1][1], pre, period)
			self._save_stats(self.flow_speed[dpid], key, speed, 5)

			# Record flows that need to be rescheduled. (hmc)
			if str(dpid).startswith('3'):
				flowDemand = speed * 8.0 / (setting.MAX_CAPACITY * 1000)
				src = stat.match['ipv4_src']
				dst = stat.match['ipv4_dst']
				if flowDemand > 0.1:
					if src not in self.hostsList:
						self.hostsList.append(src)
					if dst not in self.hostsList:
						self.hostsList.append(dst)
					self.flows.append({'src': src, 'dst': dst, 'demand': flowDemand,
						'converged':False, 'receiver_limited': False,
						'match': stat.match, 'priority': stat.priority})
					if not self.pre_GFF_path.has_key((src, dst)):
						self.pre_GFF_path[(src, dst)] = None
			else:
				pass

		# Estimate flows' demands if all the flow_stat replies are received.
		if len(self.statRecord) == 1.25 * (CONF.fanout ** 2) and self.flows:
			flows = sorted([flow for flow in self.flows], key=lambda flow: (flow['src'], flow['dst']))
			hostsList = sorted(self.hostsList)
			self._demandEstimator(flows, hostsList)
		else:
			pass

	def _demandEstimator(self, flows, hostsList):
		'''
			Estimate flows' demands.
		'''
		estimated_flows = demand_estimation(flows, hostsList)
		for flow in estimated_flows:
			if flow['demand'] > 0.1:
				self._GlobalFirstFit(flow)

	def _GlobalFirstFit(self, flow):
		'''
			Do the Hedera Global First Fit here.
			self.awareness.link_to_port = {(src_dpid,dst_dpid):(src_port,dst_port),}
			self.free_bandwidth = {dpid:{port_no:free_bw,},} Unit:Kbit/s
		'''
		src_dp = self.awareness.get_host_location(flow['src'])[0]
		dst_dp = self.awareness.get_host_location(flow['dst'])[0]
		paths = self.awareness.shortest_paths.get(src_dp).get(dst_dp)
		GFF_route = None
		for path in paths:
			fitCheck = True
			for i in xrange(len(path) - 1):
				fitCheck = False
				if self.awareness.link_to_port.has_key((path[i], path[i+1])):
					src_port = self.awareness.link_to_port[(path[i], path[i+1])][0]
					if self.free_bandwidth.has_key(path[i]) and self.free_bandwidth[path[i]].has_key(src_port):
						if (self.free_bandwidth[path[i]][src_port] / setting.MAX_CAPACITY) < flow['demand']:
							break
						else:
							fitCheck = True

			if fitCheck == True:
				GFF_route = path
				self.logger.info("[GFF PATH]%s<-->%s: %s" % (flow['src'], flow['dst'], path))
				break
		if GFF_route:
			# Install new GFF_path flow entries.
			self.logger.info("[GFF INSTALLING]%s<-->%s: %s" % (flow['src'], flow['dst'], path))
			self. _install_GFF_path(GFF_route, flow['match'], flow['priority'])

	def _install_GFF_path(self, GFF_route, match, priority):
		'''
			Installing the Global First Fit path.
			"match": {"dl_type": 2048, "in_port": 3,
					    "ipv4_src": "10.1.0.1", "ipv4_dst": "10.8.0.2"}
			flow_info = (eth_type, src_ip, dst_ip, priority)
		'''
		flow_info = (match['eth_type'], match['ipv4_src'], match['ipv4_dst'], priority)
		# Install flow entries to datapaths along the path.
		self.install_flow(self.datapaths, self.awareness.link_to_port, GFF_route, flow_info)

	def install_flow(self, datapaths, link_to_port, path, flow_info):
		'''
			Install flow entries for datapaths.
			path=[dpid1, dpid2, ...]
			flow_info = (eth_type, src_ip, dst_ip, priority)
			self.awareness.access_table = {(sw,port):(ip, mac),}
		'''
		if path is None or len(path) == 0:
			self.logger.info("Path error!")
			return
		in_port = None
		for key in self.awareness.access_table.keys():
			if self.awareness.access_table[key][0] == flow_info[1]:
				in_port = key[1]
		first_dp = datapaths[path[0]]
		out_port = first_dp.ofproto.OFPP_LOCAL
		# Install flow entry for intermediate datapaths.
		for i in xrange(1, int((len(path)-1)/2)):
			port = self.get_port_pair_from_link(link_to_port, path[i-1], path[i])
			port_next = self.get_port_pair_from_link(link_to_port, path[i], path[i+1])
			if port and port_next:
				src_port, dst_port = port[1], port_next[0]
				datapath = datapaths[path[i]]
				self.send_flow_mod(datapath, flow_info, src_port, dst_port)

		# Install flow entry for the first datapath.
		port_pair = self.get_port_pair_from_link(link_to_port, path[0], path[1])
		if port_pair is None:
			self.logger.info("Port not found in first hop.")
			return
		out_port = port_pair[0]
		self.send_flow_mod(first_dp, flow_info, in_port, out_port)

	def get_port_pair_from_link(self, link_to_port, src_dpid, dst_dpid):
		"""
			Get port pair of link, so that controller can install flow entry.
			link_to_port = {(src_dpid,dst_dpid):(src_port,dst_port),}
		"""
		if (src_dpid, dst_dpid) in link_to_port:
			return link_to_port[(src_dpid, dst_dpid)]
		else:
			self.logger.info("Link from dpid:%s to dpid:%s is not in links" %
			 (src_dpid, dst_dpid))
			return None

	def send_flow_mod(self, datapath, flow_info, src_port, dst_port):
		"""
			Build flow entry, and send it to datapath.
			flow_info = (eth_type, src_ip, dst_ip, priority)
		"""
		parser = datapath.ofproto_parser
		actions = []
		actions.append(parser.OFPActionOutput(dst_port))
		if len(flow_info) == 7:
			if flow_info[-3] == 6:
				if flow_info[-2] == 'src':
					match = parser.OFPMatch(
						in_port=src_port, eth_type=flow_info[0],
						ipv4_src=flow_info[1], ipv4_dst=flow_info[2],
						ip_proto=6, tcp_src=flow_info[-1])
				elif flow_info[-2] == 'dst':
					match = parser.OFPMatch(
						in_port=src_port, eth_type=flow_info[0],
						ipv4_src=flow_info[1], ipv4_dst=flow_info[2],
						ip_proto=6, tcp_dst=flow_info[-1])
				else:
					pass
			elif flow_info[-3] == 17:
				if flow_info[-2] == 'src':
					match = parser.OFPMatch(
						in_port=src_port, eth_type=flow_info[0],
						ipv4_src=flow_info[1], ipv4_dst=flow_info[2],
						ip_proto=17, udp_src=flow_info[-1])
				elif flow_info[-2] == 'dst':
					match = parser.OFPMatch(
						in_port=src_port, eth_type=flow_info[0],
						ipv4_src=flow_info[1], ipv4_dst=flow_info[2],
						ip_proto=17, udp_dst=flow_info[-1])
				else:
					pass
		elif len(flow_info) == 4:
			match = parser.OFPMatch(
						in_port=src_port, eth_type=flow_info[0],
						ipv4_src=flow_info[1], ipv4_dst=flow_info[2])
		else:
			pass
		priority = flow_info[3] + 1

		self.add_flow(datapath, priority, match, actions,
					  idle_timeout=15, hard_timeout=60)

	def add_flow(self, dp, priority, match, actions, idle_timeout=0, hard_timeout=0):
		"""
			Send a flow entry to datapath.
		"""
		ofproto = dp.ofproto
		parser = dp.ofproto_parser
		inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
		mod = parser.OFPFlowMod(datapath=dp, priority=priority,
								idle_timeout=idle_timeout,
								hard_timeout=hard_timeout,
								match=match, instructions=inst)
		dp.send_msg(mod)

	def _request_stats(self, datapath):
		"""
			Sending request msg to datapath
		"""
		self.logger.debug('send stats request: %016x', datapath.id)
		ofproto = datapath.ofproto
		parser = datapath.ofproto_parser
		req = parser.OFPPortDescStatsRequest(datapath, 0)
		datapath.send_msg(req)
		req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
		datapath.send_msg(req)
		req = parser.OFPFlowStatsRequest(datapath)
		datapath.send_msg(req)

	def get_min_bw_of_links(self, graph, path, min_bw):
		"""
			Getting bandwidth of path. Actually, the mininum bandwidth
			of links is the path's bandwith, because it is the bottleneck of path.
		"""
		_len = len(path)
		if _len > 1:
			minimal_band_width = min_bw
			for i in xrange(_len-1):
				pre, curr = path[i], path[i+1]
				if 'bandwidth' in graph[pre][curr]:
					bw = graph[pre][curr]['bandwidth']
					minimal_band_width = min(bw, minimal_band_width)
				else:
					continue
			return minimal_band_width
		else:
			return min_bw

	def get_best_path_by_bw(self, graph, paths):
		"""
			Get best path by comparing paths.
			Note: This function is called in EFattree module.
		"""
		capabilities = {}
		best_paths = copy.deepcopy(paths)

		for src in paths:
			for dst in paths[src]:
				if src == dst:
					best_paths[src][src] = [src]
					capabilities.setdefault(src, {src: setting.MAX_CAPACITY})
					capabilities[src][src] = setting.MAX_CAPACITY
				else:
					max_bw_of_paths = 0
					best_path = paths[src][dst][0]
					for path in paths[src][dst]:
						min_bw = setting.MAX_CAPACITY
						min_bw = self.get_min_bw_of_links(graph, path, min_bw)
						if min_bw > max_bw_of_paths:
							max_bw_of_paths = min_bw
							best_path = path
					best_paths[src][dst] = best_path
					capabilities.setdefault(src, {dst: max_bw_of_paths})
					capabilities[src][dst] = max_bw_of_paths

		# self.capabilities and self.best_paths have no actual utility in this module.
		self.capabilities = capabilities
		self.best_paths = best_paths
		return capabilities, best_paths

	def create_bw_graph(self, bw_dict):
		"""
			Save bandwidth data into networkx graph object.
		"""
		try:
			graph = self.awareness.graph
			link_to_port = self.awareness.link_to_port
			for link in link_to_port:
				(src_dpid, dst_dpid) = link
				(src_port, dst_port) = link_to_port[link]
				if src_dpid in bw_dict and dst_dpid in bw_dict:
					bw_src = bw_dict[src_dpid][src_port]
					bw_dst = bw_dict[dst_dpid][dst_port]
					bandwidth = min(bw_src, bw_dst)
					# Add key:value pair of bandwidth into graph.
					if graph.has_edge(src_dpid, dst_dpid):
						graph[src_dpid][dst_dpid]['bandwidth'] = bandwidth
					else:
						graph.add_edge(src_dpid, dst_dpid)
						graph[src_dpid][dst_dpid]['bandwidth'] = bandwidth
				else:
					if graph.has_edge(src_dpid, dst_dpid):
						graph[src_dpid][dst_dpid]['bandwidth'] = 0
					else:
						graph.add_edge(src_dpid, dst_dpid)
						graph[src_dpid][dst_dpid]['bandwidth'] = 0
			return graph
		except:
			self.logger.info("Create bw graph exception")
			if self.awareness is None:
				self.awareness = lookup_service_brick('awareness')
			return self.awareness.graph

	def _save_freebandwidth(self, dpid, port_no, speed):
		"""
			Calculate free bandwidth of port and Save it.
			port_feature = (config, state, p.curr_speed)
			self.port_features[dpid][p.port_no] = port_feature
			self.free_bandwidth = {dpid:{port_no:free_bw,},}
		"""
		port_state = self.port_features.get(dpid).get(port_no)
		if port_state:
			capacity = setting.MAX_CAPACITY   # The true bandwidth of link, instead of 'curr_speed'.
			free_bw = self._get_free_bw(capacity, speed)
			self.free_bandwidth[dpid].setdefault(port_no, None)
			self.free_bandwidth[dpid][port_no] = free_bw
		else:
			self.logger.info("Port is Down")

	def _save_stats(self, _dict, key, value, length=5):
		if key not in _dict:
			_dict[key] = []
		_dict[key].append(value)
		if len(_dict[key]) > length:
			_dict[key].pop(0)

	def _get_speed(self, now, pre, period):
		if period:
			return (now - pre) / (period)
		else:
			return 0

	def _get_free_bw(self, capacity, speed):
		# freebw: Kbit/s
		return max(capacity - speed * 8 / 1000.0, 0)

	def _get_time(self, sec, nsec):
		return sec + nsec / 1000000000.0

	def _get_period(self, n_sec, n_nsec, p_sec, p_nsec):
		return self._get_time(n_sec, n_nsec) - self._get_time(p_sec, p_nsec)

	def show_stat(self, _type):
		'''
			Show statistics information according to data type.
			_type: 'port' / 'flow'
		'''
		if setting.TOSHOW_stat is False:
			return

		bodys = self.stats[_type]
		if _type == 'flow' and setting.TOSHOW_flow_stat:
			print('\ndatapath  '
				'priority        ip_src        ip_dst  '
				'  packets        bytes  flow-speed(Kb/s)')
			print('--------  '
				'--------  ------------  ------------  '
				'---------  -----------  ----------------')
			for dpid in sorted(bodys.keys()):

				for stat in sorted([flow for flow in bodys[dpid] if ((flow.priority not in [0, 65535]) and (flow.match.get('ipv4_src')) and (flow.match.get('ipv4_dst')))],
						   key=lambda flow: (flow.priority, flow.match.get('ipv4_src'), flow.match.get('ipv4_dst'))):
					print('%8d  %8s  %12s  %12s  %9d  %11d  %16.1f' % (
						dpid,
						stat.priority, stat.match.get('ipv4_src'), stat.match.get('ipv4_dst'),
						stat.packet_count, stat.byte_count,
						abs(self.flow_speed[dpid][(stat.priority, stat.match.get('ipv4_src'), stat.match.get('ipv4_dst'))][-1])*8/1000.0))
			print

		if _type == 'port' and setting.TOSHOW_port_stat:
			print('\ndatapath  port '
				'   rx-pkts     rx-bytes ''   tx-pkts     tx-bytes '
				' port-bw(Kb/s)  port-speed(b/s)  port-freebw(Kb/s) '
				' port-state  link-state')
			print('--------  ----  '
				'---------  -----------  ''---------  -----------  '
				'-------------  ---------------  -----------------  '
				'----------  ----------')
			_format = '%8d  %4x  %9d  %11d  %9d  %11d  %13d  %15.1f  %17.1f  %10s  %10s'
			for dpid in sorted(bodys.keys()):
				for stat in sorted(bodys[dpid], key=attrgetter('port_no')):
					if stat.port_no != ofproto_v1_3.OFPP_LOCAL:
						print(_format % (
							dpid, stat.port_no,
							stat.rx_packets, stat.rx_bytes,
							stat.tx_packets, stat.tx_bytes,
							setting.MAX_CAPACITY,
							abs(self.port_speed[(dpid, stat.port_no)][-1] * 8),
							self.free_bandwidth[dpid][stat.port_no],
							self.port_features[dpid][stat.port_no][0],
							self.port_features[dpid][stat.port_no][1]))
			print
