"""Hard stops before money, sends and secrets in UI runs (ANYM-968).

The worker applies these rules to every action of a run. The rules are built in; the host
configuration's `guard` section can only add phrases, ids, packages and activities, and no
request, scenario, map or configuration field removes or disables one. A stop sends no input.
"""

import re
import time

from .config import Refused

# Criterion 2: a tap whose resolved node carries one of these labels stops (after normalize()).
TAP_PHRASES = (
    # en
    'pay', 'pay now', 'place order', 'order now', 'confirm order', 'confirm payment', 'buy now',
    'checkout and pay', 'send', 'transfer', 'top up',
    # id
    'bayar', 'bayar sekarang', 'pesan sekarang', 'pesan & antar sekarang', 'buat pesanan',
    'konfirmasi pesanan', 'konfirmasi pembayaran', 'beli sekarang', 'kirim', 'transfer', 'isi saldo',
    # ru
    'оплатить', 'заказать', 'оформить заказ', 'подтвердить заказ', 'купить', 'отправить', 'перевести',
)
# Criterion 3: a PIN or payment-code prompt anywhere on the screen stops the run.
PIN_PHRASES = ('masukkan pin', 'enter pin', 'enter your pin', 'pin gopay', 'введите pin', 'введите пин-код')
# Criterion 3: authenticators, password managers, Indonesian and Russian banks and wallets.
PROTECTED_PACKAGES = (
    # authenticators
    'com.google.android.apps.authenticator2', 'com.azure.authenticator', 'com.authy.authy',
    'com.duosecurity.duomobile', 'org.fedorahosted.freeotp', 'com.beemdevelopment.aegis',
    'com.yubico.yubioath', 'com.twofasapp',
    # password managers
    'com.x8bit.bitwarden', 'com.onepassword.android', 'com.agilebits.onepassword', 'com.lastpass.lpandroid',
    'com.dashlane', 'keepass2android.keepass2android', 'com.kunzisoft.keepass.free',
    'com.callpod.android_apps.keeper', 'proton.android.pass', 'io.enpass.app',
    'com.nordpass.android.app.password.manager',
    # Indonesian banks and wallets
    'com.bca', 'com.bca.mybca.omni.android', 'id.co.bri.brimo', 'id.bmri.livin', 'src.com.bni', 'id.bni.wondr',
    'com.bsm.activity2', 'id.co.cimbniaga.mobile.android', 'com.btpn.dc', 'com.jago.digitalBanking',
    'id.co.bankbkemobile.digitalbank', 'com.bcadigital.blu', 'net.myinfosys.PermataMobileX', 'com.gojek.gopay',
    'ovo.id', 'id.dana', 'com.telkom.mwallet',
    # Russian banks and wallets
    'ru.sberbankmobile', 'com.idamob.tinkoff.android', 'ru.vtb24.mobilebanking.android',
    'ru.alfabank.mobile.android', 'ru.raiffeisen.rmobile', 'ru.gazprombank.android.mobilebank.app',
    'com.openbank', 'ru.yoo.money',
)
# Criterion 4: messengers, mail and dialer accept no input; a send from the phone is a live action.
WRITE_DENIED_PACKAGES = (
    'com.whatsapp', 'com.whatsapp.w4b', 'org.telegram.messenger', 'org.thunderdog.challegram',
    'org.thoughtcrime.securesms', 'com.google.android.apps.messaging', 'com.google.android.gm',
    'com.facebook.orca', 'com.google.android.dialer',
)
TAP_COMMANDS = frozenset({'tapOn', 'doubleTapOn', 'longPressOn', 'tapElement'})
# Taps, typing, keys, swipes and setText. Reading (hierarchy, OCR, readList, copyTextFrom) is not input.
INPUT_COMMANDS = TAP_COMMANDS | {'setText', 'inputText', 'eraseText', 'pressKey', 'back', 'hideKeyboard',
                                 'swipe', 'scroll', 'scrollUntilVisible', 'typeInto', 'dismissPopups'}
POINT_TAP_PX = 10
OCR_BOX = (400, 240)
FOREGROUND_WAIT_S = 1.5
SENSITIVE_RULES = frozenset({'protected_package', 'pin_prompt', 'password_field'})

_SEPARATORS = r'[\s.,•\-]'
_AMOUNT = re.compile(
    rf'(?:{_SEPARATORS}*(?:\d+|(?<![^\W\d_])(?:rp|idr|usd|eur|rub|руб)(?![^\W\d_])|[$€₽]))+{_SEPARATORS}*')
_BOUNDS = re.compile(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]')


def normalize(text):
    """Case-folded, whitespace collapsed, amounts (digits, currencies and their separators) removed."""
    folded = _AMOUNT.sub(' ', str(text or '').casefold())
    return ' '.join(folded.split()).strip(' .,•-')


class Stop(Refused):
    """A definite guard stop: nothing was sent to the device for the stopped action."""

    def __init__(self, rule, matched, package=None, when='before'):
        super().__init__('guard_stopped', 409)
        self.guard = {'rule': rule, 'matched': matched, 'when': when, 'package': package,
                      'sensitive': rule in SENSITIVE_RULES}


class Rules:
    """Built-in rules plus the host configuration's additions; nothing here removes a rule."""

    def __init__(self, extra=None):
        extra = extra or {}
        lists = {key: list(extra.get(key) or ()) for key in (
            'stop_phrases', 'pin_phrases', 'protected_packages', 'write_denied_packages', 'stop_activities')}
        stop_ids: dict[str, list[str]] = {package: list(ids) for package, ids in (extra.get('stop_ids') or {}).items()}
        self.extra = {**lists, 'stop_ids': stop_ids}
        self.tap_phrases = frozenset(filter(None, map(normalize, (*TAP_PHRASES, *lists['stop_phrases']))))
        self.pin_phrases = frozenset(filter(None, map(normalize, (*PIN_PHRASES, *lists['pin_phrases']))))
        self.stop_ids = {package: frozenset(ids) for package, ids in stop_ids.items()}
        self.protected = frozenset((*PROTECTED_PACKAGES, *lists['protected_packages']))
        self.write_denied = frozenset((*WRITE_DENIED_PACKAGES, *lists['write_denied_packages']))
        self.stop_activities = frozenset(map(activity_name, lists['stop_activities']))

    def describe(self):
        return {
            'enforced': True, 'can_disable': False,
            'tap_phrases': sorted(self.tap_phrases), 'pin_phrases': sorted(self.pin_phrases),
            'stop_ids': {package: sorted(ids) for package, ids in sorted(self.stop_ids.items())},
            'protected_packages': sorted(self.protected), 'write_denied_packages': sorted(self.write_denied),
            'stop_activities': sorted(self.stop_activities),
            'configured': self.extra,
            'tap_commands': sorted(TAP_COMMANDS), 'input_commands': sorted(INPUT_COMMANDS),
            'point_tap_px': POINT_TAP_PX, 'ocr_box_px': list(OCR_BOX),
            'rules': {
                'stop_phrase': 'a tap (or a swipe within point_tap_px) whose node, nearest clickable ancestor and '
                               'their descendants carry a tap phrase in text, content-desc or resource-id entry, '
                               'compared case-folded with amounts removed',
                'stop_id': 'a tap on a node whose exact resource-id is listed for its package',
                'protected_package': 'the foreground package is protected (before every action and after the last)',
                'stop_activity': 'the foreground activity is listed',
                'pin_prompt': 'the screen shows a PIN or payment-code prompt (hierarchy text, or OCR when the '
                              'hierarchy has none and ocr_command is set)',
                'password_field': 'a visible password field',
                'write_denied': 'any input in a messenger, mail or dialer app, or launching, linking into or '
                                'clearing one',
                'unresolved_target': 'the hierarchy, foreground, link target or a point tap\'s text cannot be read',
            },
            'outcome': 'state=stopped, stop_reason=guard, guard {rule, matched, step_path, when}; the phone stays '
                       'on that screen. Show it to the owner; never retry or work around a stop.',
        }


def activity_name(value, package=None):
    """`pkg/.Act`, `pkg/full.Act` and `.Act` (with the foreground package) become `full.Act`."""
    if '/' in value:
        package, value = value.split('/', 1)
    return (package or '') + value if value.startswith('.') else value


def parse_bounds(value):
    found = _BOUNDS.fullmatch(value or '')
    return tuple(map(int, found.groups())) if found else None


def entries(node):
    """Texts a tap target carries: text, content-desc and the resource-id entry name."""
    found = [node.get('text'), node.get('content-desc')]
    resource = node.get('resource-id') or ''
    if resource:
        found.append(resource.split(':id/', 1)[-1].replace('_', ' '))
    return [value for value in found if value]


class Guard:
    def __init__(self, rules=None, ocr=None, secret_packages=frozenset()):
        # secret_packages: apps S2 bound a secret to, where a password field is expected.
        self.rules = rules or Rules()
        self.ocr, self.secret_packages = ocr, frozenset(secret_packages)

    # Device reads. Any failure is a definite stop: the guard never lets an unread screen through.
    def dump(self, device, dump=None):
        from xml.etree import ElementTree
        try:
            root = dump() if dump else ElementTree.fromstring(device.jsonrpc_call('dumpWindowHierarchy', (True, 50)))
        except Exception:  # noqa: BLE001 - failed or timed-out read: fail closed
            raise Stop('unresolved_target', 'hierarchy_unavailable') from None
        if root is None:
            raise Stop('unresolved_target', 'hierarchy_unavailable')
        return root

    def foreground(self, device):
        deadline = time.monotonic() + FOREGROUND_WAIT_S
        while True:
            try:
                current = device.app_current()
                return current.get('package'), current.get('activity')
            except Refused as error:
                if error.code != 'app_foreground_unavailable' or time.monotonic() >= deadline:
                    return None, None
            except Exception:  # noqa: BLE001 - the hierarchy's packages still decide
                return None, None
            time.sleep(0.25)

    def ocr_lines(self, device, region=None):
        if not self.ocr:
            return None
        from .screen_text import read_screen_text
        options = {'min_confidence': 0, 'limit': 300, **({'region': region} if region else {})}
        try:
            return [line['text'] for line in read_screen_text(device, options, self.ocr, None)['lines']]
        except Exception:  # noqa: BLE001 - unreadable text: fail closed
            raise Stop('unresolved_target', 'ocr_failed') from None

    # Criterion 3: screen rule, before every action and after the last one.
    def screen(self, device, when='before'):
        root = self.dump(device)
        package, activity = self.foreground(device)
        nodes = list(root.iter('node'))
        packages: set[str] = {package} if package else {node.get('package') or '' for node in nodes} - {''}
        if not packages:
            raise Stop('unresolved_target', 'foreground_unavailable', when=when)
        shown = package or sorted(packages)[0]
        for name in sorted(packages & self.rules.protected):
            raise Stop('protected_package', name, name, when)
        if activity and package and activity_name(activity, package) in self.rules.stop_activities:
            raise Stop('stop_activity', activity_name(activity, package), package, when)
        texts = [value for node in nodes for value in (node.get('text'), node.get('content-desc')) if value]
        if not texts and self.ocr:
            texts = self.ocr_lines(device) or []
        for text in texts:
            if normalize(text) in self.rules.pin_phrases:
                raise Stop('pin_prompt', normalize(text), shown, when)
        if not packages & self.secret_packages and any(
                node.get('password') == 'true' and node.get('visible-to-user') != 'false' for node in nodes):
            raise Stop('password_field', 'password', shown, when)
        return {'root': root, 'package': package, 'packages': packages, 'shown': shown}

    # Criteria 3 and 4 before each action; criterion 2 runs at the tap itself (tap()).
    def before(self, device, command):
        name, value = next(iter(command.items())) if isinstance(command, dict) else (command, None)
        options = value if isinstance(value, dict) else {}
        context = self.screen(device)
        denied = sorted(context['packages'] & self.rules.write_denied)
        if name in INPUT_COMMANDS and denied:
            raise Stop('write_denied', denied[0], denied[0])
        targets = []
        if name in {'launchApp', 'clearState'}:
            targets = [options.get('appId', value)]
        elif name == 'openApp':
            targets = [options['app_map']['package']]
        elif name in {'openLink', 'openDeepLink'}:
            link = options.get('link', value) if name == 'openLink' else \
                options['app_map']['deep_links'][options['name']]['url']
            targets = self.link_packages(device, link)
        for target in targets:
            if target in self.rules.write_denied:
                raise Stop('write_denied', target, context['shown'])
            if target in self.rules.protected:
                raise Stop('protected_package', target, context['shown'])
        return context

    def link_packages(self, device, link):
        """Every app that can open the link, so a chooser into a messenger stops as well."""
        packages = set(re.findall(r'(?:^|;)package=([A-Za-z][\w.]*)', link)) if link.startswith('intent:') else set()
        try:
            result = device._dev.shell2(['cmd', 'package', 'query-activities', '--brief', '-a',
                                         'android.intent.action.VIEW', '-d', link], timeout=15)
        except Exception:  # noqa: BLE001 - an unknown target: fail closed
            raise Stop('unresolved_target', 'link_target_unavailable') from None
        if result.returncode:
            raise Stop('unresolved_target', 'link_target_unavailable')
        packages.update(re.findall(r'^\s*([A-Za-z][\w.]*)/[\w.$]+\s*$', result.output, re.MULTILINE))
        return sorted(packages)

    # Criterion 2: tap rule on the resolved target. matched: the selector's node (XML element or
    # uiautomator2 info); points: the device pixels the tap lands on.
    def tap(self, device, points, matched=None, dump=None):
        root = self.dump(device, dump)
        parents = {child: parent for parent in root.iter() for child in parent}
        nodes = [node for node in root.iter('node') if parse_bounds(node.get('bounds'))]
        scope, starts = [], []
        if matched is not None:
            identity = identity_of(matched)
            starts += [node for node in nodes if all(node.get(k, '') == v for k, v in identity.items() if v or k == 'bounds')]
            if not starts and hasattr(matched, 'iter'):
                scope += list(matched.iter('node')) or [matched]
        for x, y in points:
            hit = deepest(nodes, parents, x, y)
            if hit is not None:
                starts.append(hit)
        for start in starts:
            scope += list(start.iter('node'))
            ancestor = start
            while ancestor is not None and ancestor.get('clickable') != 'true':
                ancestor = parents.get(ancestor)
            if ancestor is not None:
                scope += list(ancestor.iter('node'))
        package = next((node.get('package') for node in scope if node.get('package')), None)
        for node in scope:
            resource = node.get('resource-id')
            if resource and resource in self.rules.stop_ids.get(node.get('package') or package, ()):
                raise Stop('stop_id', resource, package)
        texts = [text for node in scope for text in entries(node)]
        if not texts and matched is None:
            # A point with no node or no readable text: read the box around it, else stop.
            texts = []
            for x, y in points:
                lines = self.ocr_lines(device, self.box(device, x, y))
                if lines is None:
                    raise Stop('unresolved_target', 'no_readable_text', package)
                texts += lines
            if not texts:
                raise Stop('unresolved_target', 'no_readable_text', package)
        for text in texts:
            if normalize(text) in self.rules.tap_phrases:
                raise Stop('stop_phrase', normalize(text), package)
        return {'checked': len(texts)}

    def box(self, device, x, y):
        width, height = device.window_size()
        left, top = max(0, min(x - OCR_BOX[0] // 2, width - 1)), max(0, min(y - OCR_BOX[1] // 2, height - 1))
        right, bottom = min(width, max(left + 1, x + OCR_BOX[0] // 2)), min(height, max(top + 1, y + OCR_BOX[1] // 2))
        return {'x': left, 'y': top, 'w': right - left, 'h': bottom - top}


def identity_of(matched):
    """XML attributes that identify the node a selector matched, from an element or uiautomator2 info."""
    if hasattr(matched, 'get') and hasattr(matched, 'attrib'):
        return {key: matched.get(key, '') for key in ('bounds', 'class', 'text', 'resource-id')}
    bounds = matched.get('visibleBounds') or matched.get('bounds') or {}
    try:
        text = '[{left},{top}][{right},{bottom}]'.format(**bounds)
    except (KeyError, IndexError):
        text = ''
    return {'bounds': text, 'class': matched.get('className') or '', 'text': matched.get('text') or '',
            'resource-id': matched.get('resourceName') or ''}


def deepest(nodes, parents, x, y):
    """The deepest node whose bounds contain the point; among equals the one drawn last."""
    best, best_depth = None, -1
    for node in nodes:
        bounds = parse_bounds(node.get('bounds'))
        if bounds is None:
            continue
        left, top, right, bottom = bounds
        if left <= x < right and top <= y < bottom:
            depth, ancestor = 0, parents.get(node)
            while ancestor is not None:
                depth, ancestor = depth + 1, parents.get(ancestor)
            if depth >= best_depth:
                best, best_depth = node, depth
    return best
