"""The single page: static/index.html with the chat feed URL filled in.

Markup, styles and scripts live in static/ (one file per panel, see index.html for the
load order) and are served by assets.static. Only the page itself is templated.
"""
import os
from string import Template

from state import CHAT_FEED_URL

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"),
          encoding="utf-8") as _f:
    PAGE = Template(_f.read()).substitute(chat_feed_url=CHAT_FEED_URL)
