# # """
# # CogNet-SDN | network/topology.py
# # Star/tree topology — s1 is core, s2-s6 are edge (NO LOOPS)
# # OpenFlow 1.3 | 8 hosts | 100 Mbps links
# # """

# # import os
# # import time
# # from mininet.net import Mininet
# # from mininet.node import OVSKernelSwitch, RemoteController
# # from mininet.link import TCLink
# # from mininet.cli import CLI
# # from mininet.log import setLogLevel, info


# # def build_network():

# #     net = Mininet(
# #         switch=OVSKernelSwitch,
# #         controller=None,
# #         link=TCLink,
# #         autoSetMacs=True,
# #         autoStaticArp=True
# #     )

# #     # ── Remote controller ────────────────────────────────
# #     info("*** Adding controller\n")
# #     c0 = net.addController(
# #         'c0',
# #         controller=RemoteController,
# #         ip='127.0.0.1',
# #         port=6653
# #     )

# #     # ── Switches ─────────────────────────────────────────
# #     # s1 = core switch, s2-s6 = edge switches (TREE, NO RING)
# #     info("*** Adding switches\n")
# #     s = {}
# #     for i in range(1, 7):
# #         s[i] = net.addSwitch(
# #             f's{i}',
# #             cls=OVSKernelSwitch,
# #             protocols='OpenFlow13',
# #             failMode='secure'
# #         )

# #     # ── Hosts ─────────────────────────────────────────────
# #     info("*** Adding hosts\n")
# #     h = {}
# #     host_ips = {
# #         1: '10.0.0.1', 2: '10.0.0.2',
# #         3: '10.0.0.3', 4: '10.0.0.4',
# #         5: '10.0.0.5', 6: '10.0.0.6',
# #         7: '10.0.0.7', 8: '10.0.0.8',
# #     }
# #     for i in range(1, 9):
# #         h[i] = net.addHost(
# #             f'h{i}',
# #             ip=f'{host_ips[i]}/24'
# #         )

# #     # ── Core links: s1 ↔ s2,s3,s4,s5,s6 (STAR — no loops) ──
# #     info("*** Adding core links (star topology)\n")
# #     #        (core, edge, delay_ms)
# #     core_links = [
# #         (1, 2, 2),
# #         (1, 3, 1),
# #         (1, 4, 3),
# #         (1, 5, 2),
# #         (1, 6, 1),
# #     ]
# #     for a, b, d in core_links:
# #         net.addLink(
# #             s[a], s[b], cls=TCLink,
# #             bw=100, delay=f'{d}ms',
# #             loss=0, max_queue_size=1000
# #         )

# #     # ── Host links ────────────────────────────────────────
# #     # h1,h2 → s2 | h3,h4 → s3 | h5,h6 → s4 | h7,h8 → s5
# #     # s6 is an extra switch (no hosts, used for multi-hop paths)
# #     info("*** Adding host links\n")
# #     host_map = [
# #         (1, 2), (2, 2),
# #         (3, 3), (4, 3),
# #         (5, 4), (6, 4),
# #         (7, 5), (8, 5),
# #     ]
# #     for hi, si in host_map:
# #         net.addLink(
# #             h[hi], s[si], cls=TCLink,
# #             bw=100, delay='1ms',
# #             loss=0, max_queue_size=1000
# #         )

# #     # ── Start network ─────────────────────────────────────
# #     info("*** Starting network\n")
# #     net.start()

# #     # ── Configure switches ────────────────────────────────
# #     info("*** Configuring switches\n")
# #     for i in range(1, 7):
# #         s[i].cmd(f'ovs-vsctl set bridge s{i} protocols=OpenFlow13')
# #         s[i].cmd(f'ovs-vsctl set bridge s{i} fail_mode=secure')
# #         s[i].cmd(f'ovs-vsctl set-controller s{i} tcp:127.0.0.1:6653')
# #         s[i].cmd(f'ovs-vsctl set bridge s{i} stp_enable=false')
# #         s[i].cmd(f'ovs-vsctl set bridge s{i} rstp_enable=false')

# #     print("\n" + "="*55)
# #     print("        CogNet-SDN Topology Running")
# #     print("="*55)
# #     print("  Switches  : s1 (core), s2-s6 (edge)")
# #     print("  Hosts     : h1-h8  (10.0.0.1 - 10.0.0.8)")
# #     print("  Topology  : STAR (loop-free)")
# #     print("  Bandwidth : 100 Mbps per link")
# #     print("  Controller: 127.0.0.1:6653")
# #     print("-"*55)
# #     print("  s1 ↔ s2,s3,s4,s5,s6")
# #     print("  h1,h2→s2 | h3,h4→s3 | h5,h6→s4 | h7,h8→s5")
# #     print("="*55 + "\n")

# #     info("*** Waiting for controller connection (8s)...\n")
# #     time.sleep(8)

# #     info("*** Starting CLI\n")
# #     CLI(net)

# #     info("*** Stopping network\n")
# #     net.stop()


# # if __name__ == '__main__':
# #     if os.getuid() != 0:
# #         print("ERROR: Please run with sudo")
# #         print("  sudo python3 network/topology.py")
# #         exit(1)
# #     setLogLevel('info')
# #     build_network()

# """
# CogNet-SDN | network/topology.py
# Ring+Hub topology — s1-s2-s3-s4-s5-s1 ring, s6 hub to s1,s2,s3,s4
# OpenFlow 1.3 | 8 hosts | 100 Mbps links
# Loop prevention: controller handles all forwarding (no flooding)
# """

# import os
# import time
# from mininet.net import Mininet
# from mininet.node import OVSKernelSwitch, RemoteController
# from mininet.link import TCLink
# from mininet.cli import CLI
# from mininet.log import setLogLevel, info


# def build_network():

#     net = Mininet(
#         switch=OVSKernelSwitch,
#         controller=None,
#         link=TCLink,
#         autoSetMacs=True,
#         autoStaticArp=True
#     )

#     # ── Remote controller ────────────────────────────────
#     info("*** Adding controller\n")
#     c0 = net.addController(
#         'c0',
#         controller=RemoteController,
#         ip='127.0.0.1',
#         port=6653
#     )

#     # ── Switches ─────────────────────────────────────────
#     info("*** Adding switches\n")
#     s = {}
#     for i in range(1, 7):
#         s[i] = net.addSwitch(
#             f's{i}',
#             cls=OVSKernelSwitch,
#             protocols='OpenFlow13',
#             failMode='secure'
#         )

#     # ── Hosts ─────────────────────────────────────────────
#     info("*** Adding hosts\n")
#     h = {}
#     for i in range(1, 9):
#         h[i] = net.addHost(
#             f'h{i}',
#             ip=f'10.0.0.{i}/24',
#             mac=f'00:00:00:00:00:0{i:02d}'
#         )

#     # ── Ring links: s1-s2-s3-s4-s5-s1 ────────────────────
#     info("*** Adding ring links\n")
#     ring = [(1,2,1), (2,3,2), (3,4,1), (4,5,3), (5,1,2)]
#     for a, b, d in ring:
#         net.addLink(
#             s[a], s[b], cls=TCLink,
#             bw=100, delay=f'{d}ms',
#             loss=0, max_queue_size=1000
#         )

#     # ── Hub links: s6 → s1,s2,s3,s4 ─────────────────────
#     info("*** Adding hub links\n")
#     hub = [(6,1,1), (6,2,2), (6,3,1), (6,4,2)]
#     for a, b, d in hub:
#         net.addLink(
#             s[a], s[b], cls=TCLink,
#             bw=100, delay=f'{d}ms',
#             loss=0, max_queue_size=1000
#         )

#     # ── Host links ────────────────────────────────────────
#     # h1,h2→s1 | h3,h4→s3 | h5,h6→s4 | h7,h8→s5
#     info("*** Adding host links\n")
#     host_map = [(1,1),(2,1),(3,3),(4,3),(5,4),(6,4),(7,5),(8,5)]
#     for hi, si in host_map:
#         net.addLink(
#             h[hi], s[si], cls=TCLink,
#             bw=100, delay='1ms',
#             loss=0, max_queue_size=1000
#         )

#     # ── Start network ─────────────────────────────────────
#     info("*** Starting network\n")
#     net.start()

#     # ── Configure switches ────────────────────────────────
#     info("*** Configuring switches\n")
#     for i in range(1, 7):
#         s[i].cmd(f'ovs-vsctl set bridge s{i} protocols=OpenFlow13')
#         s[i].cmd(f'ovs-vsctl set bridge s{i} fail_mode=secure')
#         s[i].cmd(f'ovs-vsctl set-controller s{i} tcp:127.0.0.1:6653')
#         s[i].cmd(f'ovs-vsctl set bridge s{i} stp_enable=false')
#         s[i].cmd(f'ovs-vsctl set bridge s{i} rstp_enable=false')

#     print("\n" + "="*55)
#     print("        CogNet-SDN Topology Running")
#     print("="*55)
#     print("  Topology  : RING + HUB")
#     print("  Switches  : s1-s6  (OpenFlow 1.3)")
#     print("  Hosts     : h1-h8  (10.0.0.1 - 10.0.0.8)")
#     print("  Bandwidth : 100 Mbps per link")
#     print("  Delay     : 1-3 ms per link")
#     print("  Controller: 127.0.0.1:6653")
#     print("-"*55)
#     print("  Ring → s1-s2-s3-s4-s5-s1")
#     print("  Hub  → s6 connects to s1,s2,s3,s4")
#     print("  h1,h2→s1 | h3,h4→s3 | h5,h6→s4 | h7,h8→s5")
#     print("="*55 + "\n")

#     info("*** Waiting for controller (8s)...\n")
#     time.sleep(8)

#     info("*** Starting CLI\n")
#     CLI(net)

#     info("*** Stopping network\n")
#     net.stop()


# if __name__ == '__main__':
#     if os.getuid() != 0:
#         print("ERROR: Please run with sudo")
#         exit(1)
#     setLogLevel('info')
#     build_network()

"""
CogNet-SDN | network/topology.py
Ring+Hub topology — s1-s2-s3-s4-s5-s1 ring, s6 hub to s1,s2,s3,s4
OpenFlow 1.3 | 8 hosts | 100 Mbps links
"""

import os
import time
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


def build_network():

    net = Mininet(
        switch=OVSKernelSwitch,
        controller=None,
        link=TCLink,
        autoSetMacs=True,
        autoStaticArp=False
    )

    info("*** Adding controller\n")
    c0 = net.addController(
        'c0',
        controller=RemoteController,
        ip='127.0.0.1',
        port=6653
    )

    info("*** Adding switches\n")
    s = {}
    for i in range(1, 7):
        s[i] = net.addSwitch(
            f's{i}',
            cls=OVSKernelSwitch,
            protocols='OpenFlow13',
            failMode='secure'
        )

    info("*** Adding hosts\n")
    h = {}
    for i in range(1, 9):
        h[i] = net.addHost(
            f'h{i}',
            ip=f'10.0.0.{i}/24',
            mac=f'00:00:00:00:00:{i:02x}'
        )

    # Ring: s1-s2-s3-s4-s5-s1
    info("*** Adding ring links\n")
    ring = [(1,2,1), (2,3,2), (3,4,1), (4,5,3), (5,1,2)]
    for a, b, d in ring:
        net.addLink(s[a], s[b], cls=TCLink,
                    bw=100, delay=f'{d}ms', loss=0, max_queue_size=1000)

    # Hub: s6 connects to s1,s2,s3,s4
    info("*** Adding hub links\n")
    hub_links = [(6,1,1), (6,2,2), (6,3,1), (6,4,2)]
    for a, b, d in hub_links:
        net.addLink(s[a], s[b], cls=TCLink,
                    bw=100, delay=f'{d}ms', loss=0, max_queue_size=1000)

    # h1,h2->s1 | h3,h4->s3 | h5,h6->s4 | h7,h8->s5
    info("*** Adding host links\n")
    host_map = [(1,1),(2,1),(3,3),(4,3),(5,4),(6,4),(7,5),(8,5)]
    for hi, si in host_map:
        net.addLink(h[hi], s[si], cls=TCLink,
                    bw=100, delay='1ms', loss=0, max_queue_size=1000)

    info("*** Starting network\n")
    net.start()

    info("*** Configuring switches\n")
    for i in range(1, 7):
        s[i].cmd(f'ovs-vsctl set bridge s{i} protocols=OpenFlow13')
        s[i].cmd(f'ovs-vsctl set bridge s{i} fail_mode=secure')
        s[i].cmd(f'ovs-vsctl set-controller s{i} tcp:127.0.0.1:6653')
        s[i].cmd(f'ovs-vsctl set bridge s{i} stp_enable=false')
        s[i].cmd(f'ovs-vsctl set bridge s{i} rstp_enable=false')

    print("\n" + "="*55)
    print("  CogNet-SDN  |  Ring+Hub Topology")
    print("="*55)
    print("  Ring  -> s1-s2-s3-s4-s5-s1")
    print("  Hub   -> s6 connects to s1,s2,s3,s4")
    print("  Hosts -> h1,h2->s1 | h3,h4->s3 | h5,h6->s4 | h7,h8->s5")
    print("  Waiting 15s for LLDP discovery, then run: pingall")
    print("="*55 + "\n")

    time.sleep(15)

    info("*** Starting CLI\n")
    CLI(net)

    info("*** Stopping network\n")
    net.stop()


if __name__ == '__main__':
    if os.getuid() != 0:
        print("ERROR: run with sudo")
        exit(1)
    setLogLevel('info')
    build_network()
