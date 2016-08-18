

from __future__ import division, absolute_import, print_function

import os
from collections import namedtuple, Counter
from itertools import chain

from beets import ui
from beets.ui import print_, input_, decargs, show_path_changes
import beets.library
from beets import autotag
from beets.autotag import hooks
from beets.autotag import Recommendation
from beets import plugins
from beets.util import syspath, normpath, displayable_path
from beets import config
from beets import importer
from beets import logging
from beets.plugins import BeetsPlugin

from flask import Flask


# Plugin hook.

class WebImportPlugin(BeetsPlugin):
    def __init__(self):
        super(WebImportPlugin, self).__init__()
        self.config.add({
            'host': u'127.0.0.1',
            'port': 8337,
            'cors': '',
        })

    def commands(self):
        cmd = ui.Subcommand('webimport', help=u'start a Web interface to manage imports')
        cmd.parser.add_option(u'-d', u'--debug', action='store_true',
                              default=False, help=u'debug mode')

        def func(lib, opts, args):
            args = ui.decargs(args)
            if args:
                self.config['host'] = args.pop(0)
            if args:
                self.config['port'] = int(args.pop(0))

            app.config['lib'] = lib
            # Enable CORS if required.
            if self.config['cors']:
                self._log.info(u'Enabling CORS with origin: {0}',
                               self.config['cors'])
                from flask.ext.cors import CORS
                app.config['CORS_ALLOW_HEADERS'] = "Content-Type"
                app.config['CORS_RESOURCES'] = {
                    r"/*": {"origins": self.config['cors'].get(str)}
                }
                CORS(app)
            # Start the web application.
            app.run(host=self.config['host'].get(unicode),
                    port=self.config['port'].get(int),
                    debug=opts.debug, threaded=True)
        cmd.func = func
        return [cmd]


app = Flask(__name__)

VARIOUS_ARTISTS = u'Various Artists'
PromptChoice = namedtuple('ExtraChoice', ['short', 'long', 'callback'])

# Global logger.
log = logging.getLogger('beets')


# Importer utilities and support.

def disambig_string(info):
    """Generate a string for an AlbumInfo or TrackInfo object that
    provides context that helps disambiguate similar-looking albums and
    tracks.
    """
    disambig = []
    if info.data_source and info.data_source != 'MusicBrainz':
        disambig.append(info.data_source)

    if isinstance(info, hooks.AlbumInfo):
        if info.media:
            if info.mediums > 1:
                disambig.append(u'{0}x{1}'.format(
                    info.mediums, info.media
                ))
            else:
                disambig.append(info.media)
        if info.year:
            disambig.append(unicode(info.year))
        if info.country:
            disambig.append(info.country)
        if info.label:
            disambig.append(info.label)
        if info.albumdisambig:
            disambig.append(info.albumdisambig)

    if disambig:
        return u', '.join(disambig)


def dist_string(dist):
    """Formats a distance (a float) as a colorized similarity percentage
    string.
    """
    out = u'%.1f%%' % ((1 - dist) * 100)
    if dist <= config['match']['strong_rec_thresh'].as_number():
        out = ui.colorize('text_success', out)
    elif dist <= config['match']['medium_rec_thresh'].as_number():
        out = ui.colorize('text_warning', out)
    else:
        out = ui.colorize('text_error', out)
    return out


def penalty_string(distance, limit=None):
    """Returns a colorized string that indicates all the penalties
    applied to a distance object.
    """
    penalties = []
    for key in distance.keys():
        key = key.replace('album_', '')
        key = key.replace('track_', '')
        key = key.replace('_', ' ')
        penalties.append(key)
    if penalties:
        if limit and len(penalties) > limit:
            penalties = penalties[:limit] + ['...']
        return ui.colorize('text_warning', u'(%s)' % ', '.join(penalties))


def show_change(cur_artist, cur_album, match):
    """Print out a representation of the changes that will be made if an
    album's tags are changed according to `match`, which must be an AlbumMatch
    object.
    """
    def show_album(artist, album):
        if artist:
            album_description = u'    %s - %s' % (artist, album)
        elif album:
            album_description = u'    %s' % album
        else:
            album_description = u'    (unknown album)'
        print_(album_description)

    def format_index(track_info):
        """Return a string representing the track index of the given
        TrackInfo or Item object.
        """
        if isinstance(track_info, hooks.TrackInfo):
            index = track_info.index
            medium_index = track_info.medium_index
            medium = track_info.medium
            mediums = match.info.mediums
        else:
            index = medium_index = track_info.track
            medium = track_info.disc
            mediums = track_info.disctotal
        if config['per_disc_numbering']:
            if mediums > 1:
                return u'{0}-{1}'.format(medium, medium_index)
            else:
                return unicode(medium_index)
        else:
            return unicode(index)

    # Identify the album in question.
    if cur_artist != match.info.artist or \
            (cur_album != match.info.album and
                     match.info.album != VARIOUS_ARTISTS):
        artist_l, artist_r = cur_artist or '', match.info.artist
        album_l,  album_r = cur_album or '', match.info.album
        if artist_r == VARIOUS_ARTISTS:
            # Hide artists for VA releases.
            artist_l, artist_r = u'', u''

        artist_l, artist_r = ui.colordiff(artist_l, artist_r)
        album_l, album_r = ui.colordiff(album_l, album_r)

        print_(u"Correcting tags from:")
        show_album(artist_l, album_l)
        print_(u"To:")
        show_album(artist_r, album_r)
    else:
        print_(u"Tagging:\n    {0.artist} - {0.album}".format(match.info))

    # Data URL.
    if match.info.data_url:
        print_(u'URL:\n    %s' % match.info.data_url)

    # Info line.
    info = []
    # Similarity.
    info.append(u'(Similarity: %s)' % dist_string(match.distance))
    # Penalties.
    penalties = penalty_string(match.distance)
    if penalties:
        info.append(penalties)
    # Disambiguation.
    disambig = disambig_string(match.info)
    if disambig:
        info.append(ui.colorize('text_highlight_minor', u'(%s)' % disambig))
    print_(' '.join(info))

    # Tracks.
    pairs = list(match.mapping.items())
    pairs.sort(key=lambda item_and_track_info: item_and_track_info[1].index)

    # Build up LHS and RHS for track difference display. The `lines` list
    # contains ``(lhs, rhs, width)`` tuples where `width` is the length (in
    # characters) of the uncolorized LHS.
    lines = []
    medium = disctitle = None
    for item, track_info in pairs:

        # Medium number and title.
        if medium != track_info.medium or disctitle != track_info.disctitle:
            media = match.info.media or 'Media'
            if match.info.mediums > 1 and track_info.disctitle:
                lhs = u'%s %s: %s' % (media, track_info.medium,
                                      track_info.disctitle)
            elif match.info.mediums > 1:
                lhs = u'%s %s' % (media, track_info.medium)
            elif track_info.disctitle:
                lhs = u'%s: %s' % (media, track_info.disctitle)
            else:
                lhs = None
            if lhs:
                lines.append((lhs, u'', 0))
            medium, disctitle = track_info.medium, track_info.disctitle

        # Titles.
        new_title = track_info.title
        if not item.title.strip():
            # If there's no title, we use the filename.
            cur_title = displayable_path(os.path.basename(item.path))
            lhs, rhs = cur_title, new_title
        else:
            cur_title = item.title.strip()
            lhs, rhs = ui.colordiff(cur_title, new_title)
        lhs_width = len(cur_title)

        # Track number change.
        cur_track, new_track = format_index(item), format_index(track_info)
        if cur_track != new_track:
            if item.track in (track_info.index, track_info.medium_index):
                color = 'text_highlight_minor'
            else:
                color = 'text_highlight'
            templ = ui.colorize(color, u' (#{0})')
            lhs += templ.format(cur_track)
            rhs += templ.format(new_track)
            lhs_width += len(cur_track) + 4

        # Length change.
        if item.length and track_info.length and \
                        abs(item.length - track_info.length) > \
                        config['ui']['length_diff_thresh'].as_number():
            cur_length = ui.human_seconds_short(item.length)
            new_length = ui.human_seconds_short(track_info.length)
            templ = ui.colorize('text_highlight', u' ({0})')
            lhs += templ.format(cur_length)
            rhs += templ.format(new_length)
            lhs_width += len(cur_length) + 3

        # Penalties.
        penalties = penalty_string(match.distance.tracks[track_info])
        if penalties:
            rhs += ' %s' % penalties

        if lhs != rhs:
            lines.append((u' * %s' % lhs, rhs, lhs_width))
        elif config['import']['detail']:
            lines.append((u' * %s' % lhs, '', lhs_width))

    # Print each track in two columns, or across two lines.
    col_width = (ui.term_width() - len(''.join([' * ', ' -> ']))) // 2
    if lines:
        max_width = max(w for _, _, w in lines)
        for lhs, rhs, lhs_width in lines:
            if not rhs:
                print_(lhs)
            elif max_width > col_width:
                print_(u'%s ->\n   %s' % (lhs, rhs))
            else:
                pad = max_width - lhs_width
                print_(u'%s%s -> %s' % (lhs, ' ' * pad, rhs))

    # Missing and unmatched tracks.
    if match.extra_tracks:
        print_(u'Missing tracks ({0}/{1} - {2:.1%}):'.format(
            len(match.extra_tracks),
            len(match.info.tracks),
            len(match.extra_tracks) / len(match.info.tracks)
        ))
    for track_info in match.extra_tracks:
        line = u' ! %s (#%s)' % (track_info.title, format_index(track_info))
        if track_info.length:
            line += u' (%s)' % ui.human_seconds_short(track_info.length)
        print_(ui.colorize('text_warning', line))
    if match.extra_items:
        print_(u'Unmatched tracks ({0}):'.format(len(match.extra_items)))
    for item in match.extra_items:
        line = u' ! %s (#%s)' % (item.title, format_index(item))
        if item.length:
            line += u' (%s)' % ui.human_seconds_short(item.length)
        print_(ui.colorize('text_warning', line))


def show_item_change(item, match):
    """Print out the change that would occur by tagging `item` with the
    metadata from `match`, a TrackMatch object.
    """
    cur_artist, new_artist = item.artist, match.info.artist
    cur_title, new_title = item.title, match.info.title

    if cur_artist != new_artist or cur_title != new_title:
        cur_artist, new_artist = ui.colordiff(cur_artist, new_artist)
        cur_title, new_title = ui.colordiff(cur_title, new_title)

        print_(u"Correcting track tags from:")
        print_(u"    %s - %s" % (cur_artist, cur_title))
        print_(u"To:")
        print_(u"    %s - %s" % (new_artist, new_title))

    else:
        print_(u"Tagging track: %s - %s" % (cur_artist, cur_title))

    # Data URL.
    if match.info.data_url:
        print_(u'URL:\n    %s' % match.info.data_url)

    # Info line.
    info = []
    # Similarity.
    info.append(u'(Similarity: %s)' % dist_string(match.distance))
    # Penalties.
    penalties = penalty_string(match.distance)
    if penalties:
        info.append(penalties)
    # Disambiguation.
    disambig = disambig_string(match.info)
    if disambig:
        info.append(ui.colorize('text_highlight_minor', u'(%s)' % disambig))
    print_(' '.join(info))


def summarize_items(items, singleton):
    """Produces a brief summary line describing a set of items. Used for
    manually resolving duplicates during import.

    `items` is a list of `Item` objects. `singleton` indicates whether
    this is an album or single-item import (if the latter, them `items`
    should only have one element).
    """
    summary_parts = []
    if not singleton:
        summary_parts.append(u"{0} items".format(len(items)))

    format_counts = {}
    for item in items:
        format_counts[item.format] = format_counts.get(item.format, 0) + 1
    if len(format_counts) == 1:
        # A single format.
        summary_parts.append(items[0].format)
    else:
        # Enumerate all the formats by decreasing frequencies:
        for fmt, count in sorted(
                format_counts.items(),
                key=lambda fmt_and_count: (-fmt_and_count[1], fmt_and_count[0])
        ):
            summary_parts.append('{0} {1}'.format(fmt, count))

    if items:
        average_bitrate = sum([item.bitrate for item in items]) / len(items)
        total_duration = sum([item.length for item in items])
        total_filesize = sum([item.filesize for item in items])
        summary_parts.append(u'{0}kbps'.format(int(average_bitrate / 1000)))
        summary_parts.append(ui.human_seconds_short(total_duration))
        summary_parts.append(ui.human_bytes(total_filesize))

    return u', '.join(summary_parts)


def _summary_judgment(rec):
    """Determines whether a decision should be made without even asking
    the user. This occurs in quiet mode and when an action is chosen for
    NONE recommendations. Return an action or None if the user should be
    queried. May also print to the console if a summary judgment is
    made.
    """
    if config['import']['quiet']:
        if rec == Recommendation.strong:
            return importer.action.APPLY
        else:
            action = config['import']['quiet_fallback'].as_choice({
                'skip': importer.action.SKIP,
                'asis': importer.action.ASIS,
            })

    elif rec == Recommendation.none:
        action = config['import']['none_rec_action'].as_choice({
            'skip': importer.action.SKIP,
            'asis': importer.action.ASIS,
            'ask': None,
        })

    else:
        return None

    if action == importer.action.SKIP:
        print_(u'Skipping.')
    elif action == importer.action.ASIS:
        print_(u'Importing as-is.')
    return action


def choose_candidate(candidates, singleton, rec, cur_artist=None,
                     cur_album=None, item=None, itemcount=None,
                     extra_choices=[]):
    """Given a sorted list of candidates, ask the user for a selection
    of which candidate to use. Applies to both full albums and
    singletons  (tracks). Candidates are either AlbumMatch or TrackMatch
    objects depending on `singleton`. for albums, `cur_artist`,
    `cur_album`, and `itemcount` must be provided. For singletons,
    `item` must be provided.

    `extra_choices` is a list of `PromptChoice`s, containg the choices
    appended by the plugins after receiving the `before_choose_candidate`
    event. If not empty, the choices are appended to the prompt presented
    to the user.

    Returns one of the following:
    * the result of the choice, which may be SKIP, ASIS, TRACKS, or MANUAL
    * a candidate (an AlbumMatch/TrackMatch object)
    * the short letter of a `PromptChoice` (if the user selected one of
    the `extra_choices`).
    """
    # Sanity check.
    if singleton:
        assert item is not None
    else:
        assert cur_artist is not None
        assert cur_album is not None

    # Build helper variables for extra choices.
    extra_opts = tuple(c.long for c in extra_choices)
    extra_actions = tuple(c.short for c in extra_choices)

    # Zero candidates.
    if not candidates:
        if singleton:
            print_(u"No matching recordings found.")
            opts = (u'Use as-is', u'Skip', u'Enter search', u'enter Id',
                    u'aBort')
        else:
            print_(u"No matching release found for {0} tracks."
                   .format(itemcount))
            print_(u'For help, see: '
                   u'http://beets.readthedocs.org/en/latest/faq.html#nomatch')
            opts = (u'Use as-is', u'as Tracks', u'Group albums', u'Skip',
                    u'Enter search', u'enter Id', u'aBort')
        sel = ui.input_options(opts + extra_opts)
        if sel == u'u':
            return importer.action.ASIS
        elif sel == u't':
            assert not singleton
            return importer.action.TRACKS
        elif sel == u'e':
            return importer.action.MANUAL
        elif sel == u's':
            return importer.action.SKIP
        elif sel == u'b':
            raise importer.ImportAbort()
        elif sel == u'i':
            return importer.action.MANUAL_ID
        elif sel == u'g':
            return importer.action.ALBUMS
        elif sel in extra_actions:
            return sel
        else:
            assert False

    # Is the change good enough?
    bypass_candidates = False
    if rec != Recommendation.none:
        match = candidates[0]
        bypass_candidates = True

    while True:
        # Display and choose from candidates.
        require = rec <= Recommendation.low

        if not bypass_candidates:
            # Display list of candidates.
            print_(u'Finding tags for {0} "{1} - {2}".'.format(
                u'track' if singleton else u'album',
                item.artist if singleton else cur_artist,
                item.title if singleton else cur_album,
            ))

            print_(u'Candidates:')
            for i, match in enumerate(candidates):
                # Index, metadata, and distance.
                line = [
                    u'{0}.'.format(i + 1),
                    u'{0} - {1}'.format(
                        match.info.artist,
                        match.info.title if singleton else match.info.album,
                    ),
                    u'({0})'.format(dist_string(match.distance)),
                ]

                # Penalties.
                penalties = penalty_string(match.distance, 3)
                if penalties:
                    line.append(penalties)

                # Disambiguation
                disambig = disambig_string(match.info)
                if disambig:
                    line.append(ui.colorize('text_highlight_minor',
                                            u'(%s)' % disambig))

                print_(u' '.join(line))

            # Ask the user for a choice.
            if singleton:
                opts = (u'Skip', u'Use as-is', u'Enter search', u'enter Id',
                        u'aBort')
            else:
                opts = (u'Skip', u'Use as-is', u'as Tracks', u'Group albums',
                        u'Enter search', u'enter Id', u'aBort')
            sel = ui.input_options(opts + extra_opts,
                                   numrange=(1, len(candidates)))
            if sel == u's':
                return importer.action.SKIP
            elif sel == u'u':
                return importer.action.ASIS
            elif sel == u'm':
                pass
            elif sel == u'e':
                return importer.action.MANUAL
            elif sel == u't':
                assert not singleton
                return importer.action.TRACKS
            elif sel == u'b':
                raise importer.ImportAbort()
            elif sel == u'i':
                return importer.action.MANUAL_ID
            elif sel == u'g':
                return importer.action.ALBUMS
            elif sel in extra_actions:
                return sel
            else:  # Numerical selection.
                match = candidates[sel - 1]
                if sel != 1:
                    # When choosing anything but the first match,
                    # disable the default action.
                    require = True
        bypass_candidates = False

        # Show what we're about to do.
        if singleton:
            show_item_change(item, match)
        else:
            show_change(cur_artist, cur_album, match)

        # Exact match => tag automatically if we're not in timid mode.
        if rec == Recommendation.strong and not config['import']['timid']:
            return match

        # Ask for confirmation.
        if singleton:
            opts = (u'Apply', u'More candidates', u'Skip', u'Use as-is',
                    u'Enter search', u'enter Id', u'aBort')
        else:
            opts = (u'Apply', u'More candidates', u'Skip', u'Use as-is',
                    u'as Tracks', u'Group albums', u'Enter search',
                    u'enter Id', u'aBort')
        default = config['import']['default_action'].as_choice({
            u'apply': u'a',
            u'skip': u's',
            u'asis': u'u',
            u'none': None,
        })
        if default is None:
            require = True
        sel = ui.input_options(opts + extra_opts, require=require,
                               default=default)
        if sel == u'a':
            return match
        elif sel == u'g':
            return importer.action.ALBUMS
        elif sel == u's':
            return importer.action.SKIP
        elif sel == u'u':
            return importer.action.ASIS
        elif sel == u't':
            assert not singleton
            return importer.action.TRACKS
        elif sel == u'e':
            return importer.action.MANUAL
        elif sel == u'b':
            raise importer.ImportAbort()
        elif sel == u'i':
            return importer.action.MANUAL_ID
        elif sel in extra_actions:
            return sel


def manual_search(singleton):
    """Input either an artist and album (for full albums) or artist and
    track name (for singletons) for manual search.
    """
    artist = input_(u'Artist:')
    name = input_(u'Track:' if singleton else u'Album:')
    return artist.strip(), name.strip()


def manual_id(singleton):
    """Input an ID, either for an album ("release") or a track ("recording").
    """
    prompt = u'Enter {0} ID:'.format(u'recording' if singleton else u'release')
    return input_(prompt).strip()



class WebImportSession(importer.ImportSession):
    """An import session that runs in a terminal.
     """
    def choose_match(self, task):
        """Given an initial autotagging of items, go through an interactive
        dance with the user to ask for a choice of metadata. Returns an
        AlbumMatch object, ASIS, or SKIP.
        """
        # Show what we're tagging.
        print_()
        print_(displayable_path(task.paths, u'\n') +
               u' ({0} items)'.format(len(task.items)))

        # Take immediate action if appropriate.
        action = _summary_judgment(task.rec)
        if action == importer.action.APPLY:
            match = task.candidates[0]
            show_change(task.cur_artist, task.cur_album, match)
            return match
        elif action is not None:
            return action

        # Loop until we have a choice.
        candidates, rec = task.candidates, task.rec
        while True:
            # Gather extra choices from plugins.
            extra_choices = self._get_plugin_choices(task)
            extra_ops = {c.short: c.callback for c in extra_choices}

            # Ask for a choice from the user.
            choice = choose_candidate(
                candidates, False, rec, task.cur_artist, task.cur_album,
                itemcount=len(task.items), extra_choices=extra_choices
            )

            # Choose which tags to use.
            if choice in (importer.action.SKIP, importer.action.ASIS,
                          importer.action.TRACKS, importer.action.ALBUMS):
                # Pass selection to main control flow.
                return choice
            elif choice is importer.action.MANUAL:
                # Try again with manual search terms.
                search_artist, search_album = manual_search(False)
                _, _, candidates, rec = autotag.tag_album(
                    task.items, search_artist, search_album
                )
            elif choice is importer.action.MANUAL_ID:
                # Try a manually-entered ID.
                search_id = manual_id(False)
                if search_id:
                    _, _, candidates, rec = autotag.tag_album(
                        task.items, search_ids=search_id.split()
                    )
            elif choice in list(extra_ops.keys()):
                # Allow extra ops to automatically set the post-choice.
                post_choice = extra_ops[choice](self, task)
                if isinstance(post_choice, importer.action):
                    # MANUAL and MANUAL_ID have no effect, even if returned.
                    return post_choice
            else:
                # We have a candidate! Finish tagging. Here, choice is an
                # AlbumMatch object.
                assert isinstance(choice, autotag.AlbumMatch)
                return choice

    def choose_item(self, task):
        """Ask the user for a choice about tagging a single item. Returns
        either an action constant or a TrackMatch object.
        """
        print_()
        print_(task.item.path)
        candidates, rec = task.candidates, task.rec

        # Take immediate action if appropriate.
        action = _summary_judgment(task.rec)
        if action == importer.action.APPLY:
            match = candidates[0]
            show_item_change(task.item, match)
            return match
        elif action is not None:
            return action

        while True:
            extra_choices = self._get_plugin_choices(task)
            extra_ops = {c.short: c.callback for c in extra_choices}

            # Ask for a choice.
            choice = choose_candidate(candidates, True, rec, item=task.item,
                                      extra_choices=extra_choices)

            if choice in (importer.action.SKIP, importer.action.ASIS):
                return choice
            elif choice == importer.action.TRACKS:
                assert False  # TRACKS is only legal for albums.
            elif choice == importer.action.MANUAL:
                # Continue in the loop with a new set of candidates.
                search_artist, search_title = manual_search(True)
                candidates, rec = autotag.tag_item(task.item, search_artist,
                                                   search_title)
            elif choice == importer.action.MANUAL_ID:
                # Ask for a track ID.
                search_id = manual_id(True)
                if search_id:
                    candidates, rec = autotag.tag_item(
                        task.item, search_ids=search_id.split())
            elif choice in extra_ops.keys():
                # Allow extra ops to automatically set the post-choice.
                post_choice = extra_ops[choice](self, task)
                if isinstance(post_choice, importer.action):
                    # MANUAL and MANUAL_ID have no effect, even if returned.
                    return post_choice
            else:
                # Chose a candidate.
                assert isinstance(choice, autotag.TrackMatch)
                return choice

    def resolve_duplicate(self, task, found_duplicates):
        """Decide what to do when a new album or item seems similar to one
        that's already in the library.
        """
        log.warn(u"This {0} is already in the library!",
                 (u"album" if task.is_album else u"item"))

        if config['import']['quiet']:
            # In quiet mode, don't prompt -- just skip.
            log.info(u'Skipping.')
            sel = u's'
        else:
            # Print some detail about the existing and new items so the
            # user can make an informed decision.
            for duplicate in found_duplicates:
                print_(u"Old: " + summarize_items(
                    list(duplicate.items()) if task.is_album else [duplicate],
                    not task.is_album,
                ))

            print_(u"New: " + summarize_items(
                task.imported_items(),
                not task.is_album,
            ))

            sel = ui.input_options(
                (u'Skip new', u'Keep both', u'Remove old')
            )

        if sel == u's':
            # Skip new.
            task.set_choice(importer.action.SKIP)
        elif sel == u'k':
            # Keep both. Do nothing; leave the choice intact.
            pass
        elif sel == u'r':
            # Remove old.
            task.should_remove_duplicates = True
        else:
            assert False

    def should_resume(self, path):
        return ui.input_yn(u"Import of the directory:\n{0}\n"
                           u"was interrupted. Resume (Y/n)?"
                           .format(displayable_path(path)))

    def _get_plugin_choices(self, task):
        """Get the extra choices appended to the plugins to the ui prompt.

        The `before_choose_candidate` event is sent to the plugins, with
        session and task as its parameters. Plugins are responsible for
        checking the right conditions and returning a list of `PromptChoice`s,
        which is flattened and checked for conflicts.

        If two or more choices have the same short letter, a warning is
        emitted and all but one choices are discarded, giving preference
        to the default importer choices.

        Returns a list of `PromptChoice`s.
        """
        # Send the before_choose_candidate event and flatten list.
        extra_choices = list(chain(*plugins.send('before_choose_candidate',
                                                 session=self, task=task)))
        # Take into account default options, for duplicate checking.
        all_choices = [PromptChoice(u'a', u'Apply', None),
                       PromptChoice(u's', u'Skip', None),
                       PromptChoice(u'u', u'Use as-is', None),
                       PromptChoice(u't', u'as Tracks', None),
                       PromptChoice(u'g', u'Group albums', None),
                       PromptChoice(u'e', u'Enter search', None),
                       PromptChoice(u'i', u'enter Id', None),
                       PromptChoice(u'b', u'aBort', None)] + \
                      extra_choices

        short_letters = [c.short for c in all_choices]
        if len(short_letters) != len(set(short_letters)):
            # Duplicate short letter has been found.
            duplicates = [i for i, count in Counter(short_letters).items()
                          if count > 1]
            for short in duplicates:
                # Keep the first of the choices, removing the rest.
                dup_choices = [c for c in all_choices if c.short == short]
                for c in dup_choices[1:]:
                    log.warn(u"Prompt choice '{0}' removed due to conflict "
                             u"with '{1}' (short letter: '{2}')",
                             c.long, dup_choices[0].long, c.short)
                    extra_choices.remove(c)
        return extra_choices

def import_files(lib, paths, query):
    """Import the files in the given list of paths or matching the
    query.
    """
    # Check the user-specified directories.
    for path in paths:
        if not os.path.exists(syspath(normpath(path))):
            raise ui.UserError(u'no such file or directory: {0}'.format(
                displayable_path(path)))

    # Check parameter consistency.
    if config['import']['quiet'] and config['import']['timid']:
        raise ui.UserError(u"can't be both quiet and timid")

    # Open the log.
    if config['import']['log'].get() is not None:
        logpath = syspath(config['import']['log'].as_filename())
        try:
            loghandler = logging.FileHandler(logpath)
        except IOError:
            raise ui.UserError(u"could not open log file for writing: "
                               u"{0}".format(displayable_path(logpath)))
    else:
        loghandler = None

    # Never ask for input in quiet mode.
    if config['import']['resume'].get() == 'ask' and \
            config['import']['quiet']:
        config['import']['resume'] = False

    session = WebImportSession(lib, loghandler, paths, query)
    session.run()

    # Emit event.
    plugins.send('import', lib=lib, paths=paths)


def import_func(lib, opts, args):
    config['import'].set_args(opts)

    # Special case: --copy flag suppresses import_move (which would
    # otherwise take precedence).
    if opts.copy:
        config['import']['move'] = False

    if opts.library:
        query = decargs(args)
        paths = []
    else:
        query = None
        paths = args
        if not paths:
            raise ui.UserError(u'no path specified')

    import_files(lib, paths, query)


@app.before_request
def before_request():
    g.lib = app.config['lib']


@app.route('/')
def hello_world():
    return 'Hello World!'


if __name__ == '__main__':
    beets.main(None)
