# Copyright (C) 2016 Huang MaChi at Chongqing University
# of Posts and Telecommunications, Chongqing, China.
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


def demand_estimation(flows, hostsList):
	"""
		Main function of demand estimation.
	"""
	M = {}
	for i in hostsList:
		M[i] = {}
		for j in hostsList:
			M[i][j] = {'demand': 0, 'pre_demand': 0, 'converged': False, 'FlowNumber': 0}

	for flow in flows:
		M[flow['src']][flow['dst']]['FlowNumber'] += 1

	demandChange = True
	while demandChange:
		demandChange = False
		for src in hostsList:
			estimate_src(M, flows, src)

		for dst in hostsList:
			estimate_dst(M, flows, dst)

		for i in hostsList:
			for j in hostsList:
				if M[i][j]['pre_demand'] != M[i][j]['demand']:
					demandChange = True
					M[i][j]['pre_demand'] = M[i][j]['demand']

	demandsPrinting(M, hostsList)
	return flows

def estimate_src(M, flows, src):
	converged_demand = 0
	unconverged_num = 0
	for flow in flows:
		if flow['src'] == src:
			if flow['converged']:
				converged_demand += flow['demand']
			else:
				unconverged_num += 1

	if unconverged_num != 0:
		equal_share = (1.0 - converged_demand) / unconverged_num
		for flow in flows:
			if flow['src'] == src and not flow['converged']:
				M[flow['src']][flow['dst']]['demand'] = equal_share
				flow['demand'] = equal_share

def estimate_dst(M, flows, dst):
	total_demand = 0
	sender_limited_demand = 0
	receiver_limited_num = 0
	for flow in flows:
		if flow['dst'] == dst:
			flow['receiver_limited'] = True
			total_demand += flow['demand']
			receiver_limited_num += 1

	if total_demand <= 1.0:
		return
	else:
		equal_share = 1.0 / receiver_limited_num
		flagFlip=True
		while flagFlip:
			flagFlip = False
			receiver_limited_num = 0
			for flow in flows:
				if flow['dst'] == dst and flow['receiver_limited']:
					if flow['demand'] < equal_share:
						sender_limited_demand += flow['demand']
						flow['receiver_limited'] = False
						flagFlip = True
					else:
						receiver_limited_num += 1
			equal_share = (1.0 - sender_limited_demand) / receiver_limited_num

		for flow in flows:
			if flow['dst'] == dst and flow['receiver_limited']:
				M[flow['src']][flow['dst']]['demand'] = equal_share
				M[flow['src']][flow['dst']]['converged'] = True
				flow['converged'] = True
				flow['demand'] = equal_share

def demandsPrinting(M, hostsList):
	"""
		Show the estimate results.
	"""
	print "********************Estimated Demands********************"
	print
	for host in hostsList:
		print host,
	print
	print  '_' * 140
	for row in hostsList:
		print row,'|',
		for col in hostsList:
			print '%.2f' % M[row][col]['demand'],
		print
	print
