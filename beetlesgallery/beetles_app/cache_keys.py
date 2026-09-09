"""Cache keys and invalidation for taxonomy-derived data.

The taxonomy tree served by the Taxonomy Browser and the species list served by
``/api/v1/species/`` are the same for every user and only change when the
reference data changes. That happens two ways:

* the ``migrate_taxonomy_to_db`` management command re-imports the CSVs, and
* (rarely) a ``Taxon`` row is edited in the Django admin.

The import command calls :func:`invalidate_taxonomy_caches` explicitly. Admin
edits are covered by ``TAXONOMY_CACHE_TTL`` as a backstop -- a ``post_save``
signal for instant admin invalidation is a reasonable follow-up but is left out
here to keep the change small and avoid a per-row signal storm during the bulk
import.
"""
from django.core.cache import cache

# Bump the ``vN`` suffix whenever the cached payload's shape changes, so a deploy
# never serves a stale-shaped value to new template / JS code.
TAXONOMY_BROWSER_CACHE_KEY = "taxonomy:browser_context:v1"
SPECIES_LIST_CACHE_KEY = "taxonomy:species_list:v1"

# Backstop expiry. Explicit invalidation (import command) is the primary path;
# this just bounds how long a missed invalidation can serve stale data.
TAXONOMY_CACHE_TTL = 60 * 60 * 6  # 6 hours

_ALL_KEYS = [TAXONOMY_BROWSER_CACHE_KEY, SPECIES_LIST_CACHE_KEY]


def invalidate_taxonomy_caches():
    """Drop every cached taxonomy payload. Safe to call when nothing is cached."""
    cache.delete_many(_ALL_KEYS)
