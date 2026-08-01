"""
Nestick Tech Lead Generator — powered by the SkelerSecurity Intelligence Engine.

A premium, high-efficiency contact & lead intelligence platform.

A single Python engine that fuses:
  * SerpApi / SERP harvesting + Google-cache fallbacks   (from clauneck.rb)
  * Stealth SERP scraping + contact-page discovery       (from script.js / Puppeteer)
  * Places-style business leads + Hunter.io enrichment   (from app.js / Electron)

Public API
----------
    from nestick import Pipeline, Settings, run

    settings = Settings(query="dentists in Lahore", pages=2)
    leads = run(settings)
"""

from __future__ import annotations

__version__ = "1.3.0"
__all__ = [
    "Settings",
    "Lead",
    "Contact",
    "Pipeline",
    "run",
    "arun",
    "Fetcher",
    "Extractor",
    "__version__",
]

from .config import Settings
from .models import Contact, Lead
from .extract import Extractor
from .http import Fetcher
from .pipeline import Pipeline, arun, run
