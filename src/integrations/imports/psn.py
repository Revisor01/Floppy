"""PlayStation Network importer for played games and hours played.

PSN exposes no clean purchase library, so "owned" is approximated by every
title the account has played (see :mod:`integrations.psn_api`). Hours played
come from ``title_stats``' cumulative play duration, which PSN reports for
every played title.

One game can appear under several title IDs -- the PS4 and PS5 releases, or
regional variants -- that all resolve to the same IGDB game, so matches are
aggregated by IGDB ID before anything is written: play durations add up,
the most recent last-played date wins.
"""

import logging
import re
from collections import defaultdict
from datetime import timedelta

from django.db import models, transaction
from django.utils import timezone

import app
from app.log_safety import exception_summary, redact_secrets
from app.models import MediaTypes, Sources, Status
from app.providers import services
from integrations import import_progress, psn_api
from integrations.imports import helpers, title_matching
from integrations.imports.helpers import MediaImportError
from integrations.models import PSNAccount

logger = logging.getLogger(__name__)

IMPORT_NOTE = "Imported from PlayStation Network"
RECENTLY_PLAYED_DAYS = 14

# last_error_message is rendered on the import page and kept until the next
# successful sync, so what lands there is scrubbed and bounded rather than
# whatever an exception happened to stringify to.
MAX_ERROR_MESSAGE_LENGTH = 500

# PSN reports a played library, so only the two library-sync modes mean
# anything here; "watchlist" and "update_collection" have nothing to act on and
# would otherwise be silently treated as "new".
SUPPORTED_MODES = frozenset({"new", "overwrite"})

# The PlayStation store decorates titles in ways IGDB doesn't
# ("It Takes Two  PS4™ & PS5™"); stripping these recovers matches. IGDB's
# PlayStation-store external IDs are numeric store product IDs, not the
# CUSA/PPSA title IDs PSN reports, so unlike Xbox's 360-era GUIDs there is no
# exact-ID fallback -- matching is by name only.
STORE_SUFFIX_RE = re.compile(
    r"\s*(?:"
    r"\([^)]*\)"
    r"|(?:[" + title_matching.DASHES + r":]\s*|\bfor\s+|\s)"
    r"(?:playstation\s*[45]|ps[45])"
    r"(?:\s*&\s*(?:playstation\s*[45]|ps[45]))*"
    r")\s*$",
    re.IGNORECASE,
)

# PSN store names use characters IGDB doesn't: single-codepoint Roman numerals
# ("FINAL FANTASY Ⅻ") and trademark symbols glued between words
# ("Gran Turismo™SPORT", which must become "Gran Turismo SPORT", not
# "Gran TurismoSPORT").
ROMAN_NUMERALS = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
                  "XI", "XII")
ROMAN_NUMERAL_NORMALIZATIONS = str.maketrans(
    {chr(0x2160 + index): numeral for index, numeral in enumerate(ROMAN_NUMERALS)},
)
SPACE_BEFORE_PUNCTUATION_RE = re.compile(r"\s+([:,!?])")


def _safe_message(message):
    """Return a message fit to persist on the account and show to the user."""
    scrubbed = redact_secrets(str(message)).strip()
    if len(scrubbed) > MAX_ERROR_MESSAGE_LENGTH:
        return scrubbed[: MAX_ERROR_MESSAGE_LENGTH - 1].rstrip() + "…"
    return scrubbed


def _normalize_name(name):
    """Replace PSN store characters that never appear in IGDB names."""
    name = name.translate(ROMAN_NUMERAL_NORMALIZATIONS)
    name = title_matching.TRADEMARK_RE.sub(" ", name)
    return SPACE_BEFORE_PUNCTUATION_RE.sub(r"\1", " ".join(name.split()))


def _search_names(name):
    """Yield search candidates for a PSN store title, most faithful first."""
    return title_matching.search_names(_normalize_name(name), STORE_SUFFIX_RE)


def importer(identifier, user, mode):
    """Import the user's played games from their connected PSN account."""
    return PSNImporter(user, mode).import_data()


class PSNImporter:
    """Import played games and hours played from PlayStation Network."""

    def __init__(self, user, mode):
        """Initialize the importer and validate account access."""
        if mode not in SUPPORTED_MODES:
            msg = (
                f"Unsupported PSN import mode {mode!r}. "
                f"Choose one of: {', '.join(sorted(SUPPORTED_MODES))}."
            )
            raise MediaImportError(msg)

        self.user = user
        self.mode = mode
        self.warnings = []

        try:
            # Fetched rather than taken off the user, whose related object is
            # cached: a second import on the same instance would otherwise
            # measure its sessions against the watermark from before the first.
            self.account = PSNAccount.objects.get(user=user)
        except PSNAccount.DoesNotExist as error:
            msg = "Connect PlayStation Network before importing"
            raise MediaImportError(msg) from error

        if not self.account.npsso:
            msg = "Connect PlayStation Network before importing"
            raise MediaImportError(msg)

        try:
            self.npsso = helpers.decrypt_or_raise(self.account.npsso)
        except MediaImportError as decrypt_error:
            self._mark_broken(str(decrypt_error))
            raise

        self.existing_media = helpers.get_existing_media(user)
        # Track media the user explicitly deleted, so it isn't recreated
        self.deleted_media = helpers.get_deleted_media(user)
        # How much of each game's cumulative PSN playtime is already on record.
        # Read from the account rather than summed from the rows: a session the
        # user deleted would otherwise be written again on the next run, and one
        # they logged by hand would swallow the next difference.
        self.watermarks = dict(self.account.synced_playtimes or {})
        # The cumulative total each queued session was measured against, so a
        # concurrent run's work can be subtracted from it before writing.
        self.session_totals = {}
        # Games whose watermark must go rather than advance, because the user
        # stopped tracking them.
        self.dropped_watermarks = set()
        self.row_totals = self._load_row_totals()
        self.protected_statuses = self._load_protected_statuses()
        self.to_update = []
        self.to_update_meta = []
        self.bulk_media = defaultdict(list)
        self.lookup_failures = 0
        # Provider errors point at IGDB; anything else is a bug on our side and
        # must not be reported as an unreachable provider.
        self.provider_failures = 0
        self.first_failure = ""

        logger.info(
            "Initialized PSN importer for user %s with mode %s",
            user.username,
            mode,
        )

    def import_data(self):
        """Import the account's played PSN titles."""
        try:
            titles, skipped = psn_api.get_played_games(self.npsso)
        except MediaImportError as error:
            self._mark_broken(str(error))
            raise
        except Exception as error:
            # psn_api translates the failures it knows about; anything else
            # would otherwise leave the account reading as connected while
            # every scheduled run keeps failing. The summary names the
            # exception type only -- the traceback goes to the log.
            logger.exception(
                "PSN library fetch failed for user %s",
                self.user.username,
            )
            msg = (
                "PSN import failed while fetching your library "
                f"({exception_summary(error)}). Check the logs for details."
            )
            self._mark_broken(msg)
            raise MediaImportError(msg) from error

        if skipped:
            # The genre heuristic behind the app filter is best-effort; a
            # misclassified game must be auditable by the user, not only
            # visible in the server log.
            self.warnings.append(
                f"Skipped {len(skipped)} non-game apps (no genres in the "
                f"PlayStation store): {', '.join(sorted(skipped))}",
            )

        if not titles:
            logger.info("No PSN titles found for user %s", self.user.username)
            # Nothing was measured, so the marks stay as they are. Writing the
            # copy read at startup would undo whatever a run that finished in
            # the meantime recorded.
            self._mark_synced(store_watermarks=False)
            return {}, "\n".join(dict.fromkeys(self.warnings))

        total = len(titles)
        aggregated = {}
        for index, title in enumerate(titles, start=1):
            import_progress.report(index, total, "PSN")
            self._process_title(title, aggregated)

        for media_id, aggregate in aggregated.items():
            self._store_game(media_id, aggregate)

        matched = len(self.bulk_media[MediaTypes.GAME.value]) + len(self.to_update)
        logger.info(
            "PSN: %d titles, %d matched, %d lookup failures (%d provider errors)",
            total,
            matched,
            self.lookup_failures,
            self.provider_failures,
        )

        if not matched and self.lookup_failures:
            if self.provider_failures == self.lookup_failures:
                msg = (
                    f"Could not reach {Sources.IGDB.label}: all "
                    f"{self.lookup_failures} of {total} PSN titles failed to "
                    f"look up. Check the {Sources.IGDB.label} credentials on "
                    f"this instance."
                )
            else:
                msg = (
                    f"All {self.lookup_failures} of {total} PSN titles failed "
                    f"to import. First error: {self.first_failure}"
                )
            self._mark_broken(msg)
            raise MediaImportError(msg)

        # Everything below writes as one unit. A run that created session rows
        # but failed before storing the watermark would report the same minutes
        # again on the next sync, so the rows and the watermark must not be able
        # to disagree. The lock serialises a manual "sync now" against the
        # scheduled run: raising a total was idempotent, adding a difference is
        # not, and two overlapping runs would otherwise book the same playtime
        # twice.
        with transaction.atomic():
            account = PSNAccount.objects.select_for_update().get(pk=self.account.pk)
            stored = account.synced_playtimes or {}
            self._rebase_sessions_on(stored)
            self._carry_protected_statuses()
            self.watermarks = self._merge_watermarks(stored)

            helpers.bulk_create_media(self.bulk_media, self.user)

            if self.to_update:
                # Only statuses are written back. Playtime now lives in its own
                # session rows, and rewriting progress from the value read
                # before the long matching phase would silently revert an edit
                # the user made while the sync ran.
                # Statuses are written with the Completed/Dropped guard enforced
                # by the database, not only by the snapshot read at the start of
                # the run: the IGDB matching phase is long, and a user marking a
                # game Completed or Dropped mid-sync must not have that clobbered
                # by a status computed from stale data.
                protected = {Status.COMPLETED.value, Status.DROPPED.value}
                pks_by_status = defaultdict(list)
                for game in self.to_update:
                    if game.status not in protected:
                        pks_by_status[game.status].append(game.pk)
                for status_value, pks in pks_by_status.items():
                    app.models.Game.objects.filter(pk__in=pks).exclude(
                        status__in=protected,
                    ).update(status=status_value)
                logger.info(
                    "Updated %d existing games for user %s",
                    len(self.to_update),
                    self.user.username,
                )

            if self.to_update_meta:
                app.models.Item.objects.bulk_update(
                    self.to_update_meta,
                    fields=["title", "image"],
                )

            self.account = account
            self._mark_synced()

        imported_counts = {
            media_type: len(media_list)
            for media_type, media_list in self.bulk_media.items()
        }
        logger.info(
            "PSN import completed for user %s: %s",
            self.user.username,
            imported_counts,
        )
        return imported_counts, "\n".join(dict.fromkeys(self.warnings))

    def _rebase_sessions_on(self, current):
        """Recompute the queued sessions against what is on record right now.

        The watermarks were read before the IGDB matching phase, which is long
        enough for another run to finish inside it. Its work shows up here as a
        higher watermark. The overlap is rarely the whole session -- the two
        runs fetched their totals at different moments -- so each session is
        measured again against the newer mark and shortened to the part nobody
        has booked yet, rather than kept or dropped whole.
        """
        kept = []
        for game in self.bulk_media[MediaTypes.GAME.value]:
            media_id = game.item.media_id
            total = self.session_totals.get(id(game))
            written = current.get(media_id)
            if total is None or written is None or written <= total - game.progress:
                kept.append(game)
                continue

            remaining = total - written
            if remaining <= 0:
                logger.info(
                    "Dropping PSN session for %s: another run already recorded "
                    "past %s minutes",
                    media_id,
                    total,
                )
                self.watermarks[media_id] = max(
                    written,
                    self.watermarks.get(media_id, 0),
                )
                continue

            logger.info(
                "Shortening PSN session for %s from %s to %s minutes: another "
                "run recorded up to %s",
                media_id,
                game.progress,
                remaining,
                written,
            )
            game.progress = remaining
            kept.append(game)
        self.bulk_media[MediaTypes.GAME.value] = kept

    def _merge_watermarks(self, current):
        """Fold this run's marks into the stored ones, never lowering a value.

        Only the games this run actually moved may advance. Everything else --
        a game whose total did not grow, one whose lookup failed, one PSN
        stopped reporting -- keeps whatever is on record, so writing the dict
        back cannot undo a mark another run just set and hand its playtime to
        the next sync a second time.
        """
        merged = dict(current)
        for media_id, minutes in self.watermarks.items():
            merged[media_id] = max(minutes, current.get(media_id, 0))
        for media_id in self.dropped_watermarks:
            merged.pop(media_id, None)
        return merged

    def _carry_protected_statuses(self):
        """Re-read the statuses a session row must not undo.

        The map built at startup predates the IGDB matching phase, which runs
        long enough for the user to mark a game Completed in the meantime. A
        session created from the stale map would show that game as in progress
        again, so the rows are checked once more inside the write lock.
        """
        protected = self._load_protected_statuses()
        if not protected:
            return
        for game in self.bulk_media[MediaTypes.GAME.value]:
            status = protected.get(game.item.media_id)
            if status is not None:
                game.status = status

    def _mark_synced(self, store_watermarks=True):
        """Record a successful sync on the account row."""
        fields = [
            "last_sync_at",
            "connection_broken",
            "last_error_message",
            "updated_at",
        ]
        self.account.last_sync_at = timezone.now()
        self.account.connection_broken = False
        self.account.last_error_message = ""
        if store_watermarks:
            self.account.synced_playtimes = self.watermarks
            fields.append("synced_playtimes")
        self.account.save(update_fields=fields)

    def _mark_broken(self, message):
        """Flag the account as needing attention, storing a scrubbed reason."""
        self.account.connection_broken = True
        self.account.last_error_message = _safe_message(message)
        self.account.save(
            update_fields=["connection_broken", "last_error_message", "updated_at"],
        )

    def _record_failure(self, detail):
        """Count a title that couldn't be processed, keeping the first reason."""
        self.lookup_failures += 1
        if not self.first_failure:
            self.first_failure = detail

    def _process_title(self, title, aggregated):
        """Match a PSN title to IGDB and fold it into the aggregate."""
        title_id = title["title_id"]
        name = title["name"] or f"Unknown Game {title_id}"

        try:
            igdb_game = self._match_with_igdb(name)
        except services.ProviderAPIError as e:
            # ProviderAPIError writes its own user-facing message and keeps the
            # response body out of it; scrub it anyway before it is persisted.
            logger.warning(
                "IGDB lookup failed for PSN title %s: %s",
                name,
                exception_summary(e),
            )
            detail = f"{Sources.IGDB.label} error: {_safe_message(e)}"
            self.warnings.append(f"{name} ({title_id}): {detail}")
            self._record_failure(f"{name}: {detail}")
            self.provider_failures += 1
            return
        except Exception as e:
            # An unexpected exception's message is not written for display: it
            # can carry the request URL, the response body, or the credentials
            # sent with it, and the first one seen ends up on the account row.
            logger.exception(
                "Failed to process PSN title %s (%s)",
                name,
                title_id,
            )
            detail = exception_summary(e)
            self.warnings.append(f"{name} ({title_id}): {detail}")
            self._record_failure(f"{name}: {detail}")
            return

        if not igdb_game:
            logger.debug(
                "Skipping PSN title %s (titleId: %s) - no IGDB match found",
                name,
                title_id,
            )
            self.warnings.append(
                f"{name} ({title_id}): Couldn't find a match in {Sources.IGDB.label}",
            )
            return

        media_id = str(igdb_game["media_id"])
        aggregate = aggregated.setdefault(
            media_id,
            {
                "title": igdb_game["title"],
                "image": igdb_game["image"],
                "minutes": 0,
                "last_played": None,
            },
        )
        aggregate["minutes"] += title["minutes"]
        last_played = title["last_played"]
        if last_played and (
            aggregate["last_played"] is None
            or last_played > aggregate["last_played"]
        ):
            aggregate["last_played"] = last_played

    def _game_rows(self):
        """Return the user's IGDB game rows, the only ones PSN ever writes."""
        return app.models.Game.objects.filter(
            user=self.user,
            item__media_type=MediaTypes.GAME.value,
            item__source=Sources.IGDB.value,
        )

    def _load_row_totals(self):
        """Sum the tracked minutes per game, used to seed a missing watermark.

        Seeding from the rows rather than from PSN keeps an incomplete run --
        one where a sibling title ID failed to look up -- from inventing a
        session for playtime that was already on record.
        """
        totals = self._game_rows().values("item__media_id").annotate(
            total=models.Sum("progress"),
        )
        return {row["item__media_id"]: row["total"] or 0 for row in totals}

    def _load_protected_statuses(self):
        """Map each game to a Completed/Dropped status the user set by hand.

        A session row carries the status forward: the list shows the status of
        whichever row was active last, so a fresh session would otherwise pull
        a game the user marked Completed back to In progress.
        """
        protected = (
            self._game_rows()
            .filter(status__in=[Status.COMPLETED.value, Status.DROPPED.value])
            .values_list("item__media_id", "status")
        )
        return dict(protected)

    def _store_game(self, media_id, aggregate):
        """Create or update the game a set of PSN titles resolved to."""
        if (
            media_id in self.deleted_media[MediaTypes.GAME.value][Sources.IGDB.value]
            and media_id not in self.row_totals
        ):
            # PSN keeps reporting a title forever once it has been launched,
            # so without this every scheduled sync resurrects a game the user
            # deleted here on purpose. The tombstone is recorded per item, and
            # deleting one session of a game still writes it -- so a game that
            # kept rows was not untracked, only pruned, and must keep syncing.
            logger.debug(
                "Skipping deleted PSN game: %s (%s) - deleted locally",
                aggregate["title"],
                media_id,
            )
            # Drop the watermark too. If the user tracks the game again later,
            # a stale one would turn every hour played in the meantime into a
            # single phantom session.
            self.watermarks.pop(media_id, None)
            self.dropped_watermarks.add(media_id)
            return

        minutes = aggregate["minutes"]
        last_played = aggregate["last_played"]
        existing = self.existing_media[MediaTypes.GAME.value][Sources.IGDB.value].get(
            media_id,
        )

        if existing:
            # PSN reports one cumulative total per game, never a session list,
            # so a session is the growth since the last run. The watermark is
            # what that growth is measured against; without one -- a game
            # tracked before this importer wrote sessions, or a reconnected
            # account -- the rows already on record stand in for it.
            recorded = self.watermarks.get(media_id)
            first_sync = recorded is None
            if first_sync:
                recorded = self.row_totals.get(media_id, existing.progress)
            delta = minutes - recorded

            if delta > 0:
                # Without a watermark this run is catching up on a history PSN
                # kept all along, not reporting a session: the game was tracked
                # before sessions existed, added by hand, or synced from another
                # source. Dating that would drop years of playtime onto the day
                # PSN last saw the game, so it is left undated like a first
                # import. Only later runs, which measure against a mark this
                # importer set, describe playtime it actually watched happen.
                self._add_session(
                    media_id,
                    existing.item,
                    delta,
                    None if first_sync else last_played,
                    minutes,
                )
                self.watermarks[media_id] = minutes
            else:
                # A total below what is on record never means the user played
                # less: PSN's duration only grows. It means incomplete data --
                # a sibling title ID whose IGDB lookup failed this run, or a
                # playDuration PSN reports as absent (psnawp collapses that to
                # zero). Write nothing and leave the watermark where it is, so
                # the next complete run measures against the truth.
                self.watermarks[media_id] = recorded
                if self.mode == "overwrite":
                    self._refresh_status(existing, last_played)

            item = existing.item
            item.title = aggregate["title"]
            item.image = aggregate["image"]
            self.to_update_meta.append(item)
            return

        item, _ = app.models.Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            defaults={"title": aggregate["title"], "image": aggregate["image"]},
        )
        # A game seen for the first time carries its whole history as one
        # undated row. Dating it would drop years of playtime onto whichever
        # day PSN last saw it, which reads as a single marathon session.
        first_row = app.models.Game(
            item=item,
            user=self.user,
            status=self._determine_game_status(minutes, last_played),
            score=None,
            progress=minutes,
            notes=IMPORT_NOTE,
            start_date=None,
            end_date=None,
        )
        self.bulk_media[MediaTypes.GAME.value].append(first_row)
        # Recorded like a session so the write phase can measure it again: two
        # first runs starting together both hold the whole history, and without
        # this the second one books it a second time.
        self.session_totals[id(first_row)] = minutes
        self.watermarks[media_id] = minutes

    def _add_session(self, media_id, item, minutes, last_played, total):
        """Queue the playtime added since the last sync as its own row."""
        played_at = last_played
        if played_at and timezone.is_naive(played_at):
            played_at = timezone.make_aware(played_at)
        status = self.protected_statuses.get(media_id)
        if status is None:
            status = self._determine_game_status(minutes, last_played)
        session = app.models.Game(
            item=item,
            user=self.user,
            status=status,
            score=None,
            progress=minutes,
            notes=IMPORT_NOTE,
            start_date=None,
            end_date=played_at,
        )
        self.bulk_media[MediaTypes.GAME.value].append(session)
        self.session_totals[id(session)] = total

    def _refresh_status(self, existing, last_played):
        """Re-evaluate an untouched game's status without moving its playtime."""
        if existing.status in {Status.COMPLETED.value, Status.DROPPED.value}:
            return
        existing.status = self._determine_game_status(
            existing.progress,
            last_played,
        )
        self.to_update.append(existing)

    def _determine_game_status(self, minutes, last_played):
        """Determine game status from PSN playtime and last played date.

        Args:
            minutes (int): Total minutes played across all title IDs
            last_played (datetime | None): When the game was last launched

        Returns:
            str: Status value from Status choices
        """
        # Never meaningfully launched.
        if not minutes and last_played is None:
            return Status.PLANNING.value

        if last_played is not None:
            cutoff = timezone.now() - timedelta(days=RECENTLY_PLAYED_DAYS)
            if timezone.is_naive(last_played):
                last_played = timezone.make_aware(last_played)
            if last_played >= cutoff:
                return Status.IN_PROGRESS.value

        return Status.PAUSED.value

    def _match_with_igdb(self, name):
        """Match a PSN title to IGDB by name.

        PSN's CUSA/PPSA title IDs have no IGDB counterpart (IGDB's
        PlayStation-store external IDs are numeric store product IDs), so
        name search is the only option.
        """
        # Pin the source: the Item below is written as IGDB either way.
        for candidate in _search_names(name):
            results = services.search(
                MediaTypes.GAME.value,
                candidate,
                1,
                source=Sources.IGDB.value,
            ).get("results", [])
            if not results:
                continue

            match = results[0]
            logger.info(
                "Matched PSN title %s with IGDB ID %s by name %r",
                name,
                match["media_id"],
                candidate,
            )
            return {
                "media_id": match["media_id"],
                "title": match.get("title", name),
                "image": match["image"],
            }

        return None
