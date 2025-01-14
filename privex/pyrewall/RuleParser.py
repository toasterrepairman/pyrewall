import re
import logging
from ipaddress import ip_network, IPv4Network, IPv6Network
from typing import List, Tuple, Optional
from privex.helpers import is_true, empty
from privex.pyrewall.RuleBuilder import RuleBuilder
from privex.pyrewall.exceptions import RuleSyntaxError, InvalidPort
from privex.pyrewall.core import valid_port
from privex.pyrewall.types import IPT_TYPE, IPT_ACTION
from privex.pyrewall import conf

log = logging.getLogger(__name__)


class RuleParser:
    """
    RuleParser - Parses individual PyreWall rules such as ``allow port 22`` and converts them into
    iptables format.

    Basic usage:

        >>> r = RuleParser()
        >>> v4r, v6r = r.parse('allow port 22 from 192.168.0.0/16')
        >>> print(v4r)
        ['-A INPUT -p tcp --dport 22 -s 192.168.0.0/16 -j ACCEPT']
        >>> print(v6r) # As the rule explicitly specifies an IPv4 CIDR, only IPv4 rules were generated
        []

    """
    default_action: IPT_ACTION
    action: IPT_ACTION
    rule_type: str
    table: str
    v4_rules: List[str]
    v6_rules: List[str]
    rgx_ports = re.compile(r'([0-9]+,?)+')
    # protocol: str
    rule: RuleBuilder

    def __init__(self, rule_type: str = IPT_TYPE.INPUT.value, table='filter', strict=False):
        self.table = table
        self.rule_type = str(rule_type)
        self.default_action = IPT_ACTION.ALLOW
        self.rule = None
        self.chains = dict(conf.DEFAULT_CHAINS[self.table])
        self.strict = is_true(strict)
        self.has_v4, self.has_v6 = False, False
        self.reset_rule()
        # self.protocol = None

    def reset_rule(self):
        log.debug('Resetting RuleBuilder...')
        del self.rule
        self.rule = RuleBuilder(rule_type=self.rule_type)
        self.has_v4, self.has_v6 = False, False
        return self.rule

    def parse_ports(self, *args, protocol=None, **kwargs):
        protocol = self.rule.protocol if protocol is None else protocol
        args = list(args)

        for i, a in enumerate(args[0:2]):
            a = str(a)
            if a.lower() in ['tcp', 'udp', 'both']:
                args.pop(i)
                protocol = a.lower() if protocol is not False else False
                continue

        try:
            a = args[0]
        except (KeyError, IndexError):
            raise RuleSyntaxError('No ports passed to PORT rule...')

        arg_valid = RuleParser.rgx_ports.match(a)
        if len(arg_valid.groups()) == 0:
            raise RuleSyntaxError(f"Syntax error while parsing argument to PORT rule: '{a}'")

        if protocol is not False and self.rule.protocol is None:
            protocol = 'tcp' if empty(protocol) else protocol
            if protocol == 'both':
                self.rule.protocol = 'tcp'
                self.rule.extra_protocols.append('udp')
            else:
                self.rule.protocol = protocol

        ports = []
        _ports = a.split(',')
        for p in _ports:
            try:
                multi = ':' in p or '-' in p
                if multi:
                    splitchar = ':' if ':' in p else '-'
                    p_start, p_end = p.split(splitchar, maxsplit=2)
                    p_start, p_end = valid_port(p_start), valid_port(p_end)
                    ports += [f'{int(p_start)}:{int(p_end)}']
                    continue
                ports += [str(valid_port(p))]
            except InvalidPort as e:
                if self.strict:
                    raise RuleSyntaxError(f"(Strict Mode) An invalid port '{p}' was present in your rule.")
                log.warning('WARNING: Invalid port in rule. Ignoring port. Message: %s', str(e))
            continue

        if len(ports) == 0:
            raise RuleSyntaxError('No valid ports passed to PORT rule...')

        return ports, args

    def handle_port(self, *args, protocol=None, **kwargs):
        args = list(args)
        ports, args = self.parse_ports(*args, protocol=protocol, **kwargs)
        self.rule.ports += ports
        args.pop(0)
        return args

    def handle_sport(self, *args, protocol=None, **kwargs):
        args = list(args)
        ports, args = self.parse_ports(*args, protocol=protocol, **kwargs)
        self.rule.sports += ports
        args.pop(0)
        return args

    def parse(self, rule: str, reset_rule=True) -> Tuple[Optional[List[str]], Optional[List[str]]]:
        rule = rule.strip()
        if rule[0] == '#': return [], []

        rule = list(rule.split())

        while len(rule) > 0:
            rl = rule.pop(0)
            if rl.strip()[0] == '#':
                log.debug('Final rule word "%s" appears to be a comment. Breaking while loop.', rl)
                break
            if rl in self.rule_handlers:
                log.debug('Handler "%s" detected. Passing remaining rule to handler.', rl)
                rule = list(self.rule_handlers[rl](self, *rule))
                continue
            log.warning('WARNING: No known handler for keyword "%s". Ignoring.', rl)
            return None, None

        if not self.has_v4 and not self.has_v6:
            res = list(self.rule.build())
            out = res, res
        else:
            res = dict(v4=[], v6=[])
            if self.has_v4:
                res['v4'] = list(self.rule.build('v4'))
            if self.has_v6:
                res['v6'] = list(self.rule.build('v6'))
            out = res['v4'], res['v6']

        if reset_rule:
            self.reset_rule()

        return out

    def handle_from(self, *args, **kwargs):
        args = list(args)
        ip4, ip6 = self._parse_ips(args.pop(0))
        self.has_v4, self.has_v6 = len(ip4) > 0 or self.has_v4, len(ip6) > 0 or self.has_v6
        self.rule.add_from_cidr(*ip4, ipver='v4')
        self.rule.add_from_cidr(*ip6, ipver='v6')
        return args

    def handle_to(self, *args, **kwargs):
        args = list(args)
        ip4, ip6 = self._parse_ips(args.pop(0))
        self.has_v4, self.has_v6 = len(ip4) > 0 or self.has_v4, len(ip6) > 0 or self.has_v6
        self.rule.add_to_cidr(*ip4, ipver='v4')
        self.rule.add_to_cidr(*ip6, ipver='v6')
        return args

    def handle_if_in(self, *args, **kwargs):
        args = list(args)
        ifaces = args.pop(0)
        self.rule.add_from_iface(*ifaces.split(','))
        return args

    def handle_if_out(self, *args, **kwargs):
        args = list(args)
        ifaces = args.pop(0)
        self.rule.add_to_iface(*ifaces.split(','))
        return args

    def _parse_ips(self, ips: str):
        ips = ips.split(',')
        ips = [ip_network(ip, strict=self.strict) for ip in ips]
        ip4 = [ip for ip in ips if isinstance(ip, IPv4Network)]
        ip6 = [ip for ip in ips if isinstance(ip, IPv6Network)]
        return ip4, ip6

    def handle_allow(self, *args, **kwargs):
        self.rule.action = IPT_ACTION.ALLOW
        return args

    def handle_drop(self, *args, **kwargs):
        self.rule.action = IPT_ACTION.DROP
        return args

    def handle_reject(self, *args, **kwargs):
        self.rule.action = IPT_ACTION.REJECT
        return args

    def handle_forward(self, *args, **kwargs):
        self.rule.rule_type = IPT_TYPE.FORWARD.value
        return args

    def handle_output(self, *args, **kwargs):
        self.rule.rule_type = IPT_TYPE.OUTPUT.value
        return args

    def handle_state(self, *args, **kwargs):
        args = list(args)
        _state = args.pop(0).split(',')

        for i, state in enumerate(_state):
            if state in ['invalid', 'new', 'related', 'established']: _state[i] = state.upper()

        self.rule.match_rules.append(f'-m state --state {",".join(_state)}')
        return args

    def handle_all(self, *args):
        curr_type = str(self.rule.rule_type)
        ftypes = [rtype for rtype in self.chains if f'-A {rtype}' != curr_type]
        self.rule.add_rule_type(*ftypes)
        return list(args)

    def handle_chain(self, *args):
        args = list(args)
        chains = args.pop(0).split(',')
        chains = [c.upper() for c in chains if c in ['input', 'forward', 'output', 'postrouting', 'prerouting']]
        self.rule.rule_type = f'-A {chains[0]}'
        if len(chains) > 1:
            self.rule.add_rule_type(*chains[1:])
        return args

    rule_handlers = {
        'port': handle_port,
        'sport': handle_sport,
        'allow': handle_allow,
        'accept': handle_allow,
        'drop': handle_drop,
        'reject': handle_reject,
        'forward': handle_forward,
        'from': handle_from,
        'to': handle_to,
        'if-in': handle_if_in,
        'if-out': handle_if_out,
        'state': handle_state,
        'all': handle_all,
        'chain': handle_chain,
    }
