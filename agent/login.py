"""Auth, world connect and character login (#292: moved out of agent/__main__.py, unchanged).

connect_and_login() is the reconnect supervisor's build_session(): each call builds a new
WoWSession, and with it a new WorldState."""

from .auth import auth_logon
from .channels import parse_channel_spec
from .config import get as config_get
from . import death  # noqa: F401  registers release_spirit/reclaim_corpse (gh-321); every entry point logs in through this module
from .session import WoWSession


def authenticate_and_login(cfg, log) -> tuple:
    """Auth against the auth server, connect to the world server, and list
    characters. Returns (session, chars) with the session logged in only as
    far as the world connection — no character selected yet. Raises on any
    failure (AuthRejected, a socket error, ...) —
    callers decide how to react (main()'s --list-chars path exits;
    connect_and_login()/the reconnect supervisor propagate it up)."""
    log.info("authenticating to %s:%d as %s", cfg.wow_host, cfg.wow_auth_port, cfg.account)
    account, session_key, realms = auth_logon(cfg.wow_host, cfg.wow_auth_port,
                                               cfg.account, cfg.password)

    r = list(realms.values())[0]
    world_host, port_s = r['address'].rsplit(':', 1)
    world_port = int(port_s)
    log.info("realm: %s @ %s:%d", r['name'], world_host, world_port)

    sess = WoWSession(world_host, world_port, account, session_key, r['id'],
                      verbose_packets=cfg.verbose_packets,
                      dump_packets_dir=cfg.dump_packets_dir)
    sess.connect()
    chars = sess.enum_characters()
    for c in chars:
        log.info("  [%d] %-16s L%-2d class=%-2d race=%-2d",
                 c['guid'], c['name'], c['level'], c['class_'], c['race'])
    return sess, chars


def resolve_character(cfg, chars: list) -> dict:
    choice = None
    if cfg.char_guid:
        choice = next((c for c in chars if c['guid'] == cfg.char_guid), None)
    if not choice and cfg.character:
        choice = next((c for c in chars if c['name'].lower() == cfg.character.lower()), None)
    if not choice and chars:
        choice = chars[0]
    if not choice:
        raise RuntimeError("no characters on realm (and nothing to pick)")
    return choice


def connect_and_login(cfg, log, chat_relay=None) -> WoWSession:
    """Full auth -> connect -> character login sequence, returning a
    WoWSession already logged in as the configured (or first available)
    character. Raises on failure. Used directly by main() for --dry-run,
    and as the reconnect supervisor's build_session() for the default
    run-forever mode (UM-43) — each call builds a brand-new WoWSession, and
    therefore a brand-new WorldState (agent.perception), from scratch."""
    sess, chars = authenticate_and_login(cfg, log)
    sess.chat_relay = chat_relay  # UM-47 — attached before login so nothing is missed
    choice = resolve_character(cfg, chars)
    log.info("logging in: %s (guid %d)", choice['name'], choice['guid'])
    sess.agent_names = frozenset(n.strip().lower() for n in config_get('AGENT_ROSTER').split(',') if n.strip())  # #318
    sess.race = choice['race']
    sess.class_ = choice['class_']
    sess.login_character(choice['guid'])
    log.info("online — guid %d, position %s", sess.player_guid, sess.player_position)
    # UM-93: the server doesn't auto-join General at login (a real client
    # asks for it), so join the configured channels now. A failed join is
    # logged and otherwise ignored. Joining is kept with chat sending deferred
    # (UM-98) so channel chat is still heard (chat_inbox, chat-feed relay).
    sess.join_channels(parse_channel_spec(getattr(cfg, "channels", None)))
    return sess
