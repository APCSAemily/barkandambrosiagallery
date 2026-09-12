"""Cache keys and invalidation for the expensive, mostly-static page payloads.

Two unrelated things are cached here, with two different invalidation
strategies because the underlying data changes very differently:

Taxonomy (Taxonomy Browser tree + the unfiltered /api/v1/species/ list)
-------------------------------------------------------------------------
Identical for every visitor and only changes when the reference data changes,
which happens through exactly one entry point: the ``migrate_taxonomy_to_db``
management command re-importing the CSVs. That command calls
:func:`invalidate_taxonomy_caches` explicitly, so it's the primary
invalidation path. A rare direct ``Taxon`` edit in the Django admin is covered
by ``TAXONOMY_CACHE_TTL`` as a backstop -- a ``post_save`` signal for instant
admin invalidation is a reasonable follow-up, left out here to keep the change
small and avoid a per-row signal storm during the bulk import.

Gallery default-view filter options
-------------------------------------------------------------------------
The Image Browser's filter dropdowns (institution, country, species, ...) cost
~1-2 queries each (22 filters -> ~47 queries) and were rebuilt on every page
load. Unlike taxonomy, specimen/image data changes through many different,
frequent code paths -- uploads, single-record edits in the annotate tool,
deletes -- with no single "the data just changed" hook to invalidate from.
So this one is deliberately TTL-only, and deliberately short: a newly added
value (e.g. a new institution) can take up to ``GALLERY_DEFAULT_FILTERS_TTL``
to show up in the dropdown, which is a fine trade for not rebuilding it on
every visit. Only the *default* view (no search, no filter selected) is
cached at all -- see gallery()'s "Build Dynamic Options" comment for why the
filtered/searched case can't be cached the same way.
"""
from django.core.cache import cache

# Bump the ``vN`` suffix whenever a cached payload's shape changes, so a deploy
# never serves a stale-shaped value to new template / JS code.
TAXONOMY_BROWSER_CACHE_KEY = "taxonomy:browser_context:v1"
SPECIES_LIST_CACHE_KEY = "taxonomy:species_list:v1"
GALLERY_DEFAULT_FILTERS_CACHE_KEY = "gallery:default_filter_context:v1"

# Backstop expiry for taxonomy. Explicit invalidation (import command) is the
# primary path; this just bounds how long a missed invalidation can serve
# stale data.
TAXONOMY_CACHE_TTL = 60 * 60 * 6  # 6 hours

# Primary (only) invalidation path for the gallery filter cache -- short on
# purpose, see module docstring.
GALLERY_DEFAULT_FILTERS_TTL = 60 * 5  # 5 minutes

_ALL_TAXONOMY_KEYS = [TAXONOMY_BROWSER_CACHE_KEY, SPECIES_LIST_CACHE_KEY]


def invalidate_taxonomy_caches():
    """Drop every cached taxonomy payload. Safe to call when nothing is cached."""
    cache.delete_many(_ALL_TAXONOMY_KEYS)


def invalidate_gallery_filter_cache():
    """Drop the cached default-view gallery filter options.

    Not called anywhere automatically (see module docstring) -- available for
    tests, and for a management command / view to call after a bulk data
    change if waiting out the TTL isn't acceptable.
    """
    cache.delete(GALLERY_DEFAULT_FILTERS_CACHE_KEY)
