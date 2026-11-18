"""Static checks for the PerunChat friends actions in the dashboard.

Locked design (``perunchat-friends-and-aliases.md``): a friend row offers
**Chat** (open/create the bilateral private room) and **Add to room…** (invite
the friend into an existing private/group room). Both must go through the
single "open room in chat" helper, never a second join path.
"""

from __future__ import annotations

from perun import config

JS = config.REPO_ROOT / "web" / "js" / "perun.js"


def _src() -> str:
    return JS.read_text(encoding="utf-8")


def test_friend_row_offers_chat_and_add_to_room():
    src = _src()
    assert 'data-chat="${esc(id)}"' in src
    assert 'data-addroom="${esc(id)}"' in src
    assert ">Chat</button>" in src
    assert "Add to room…" in src


def test_chat_button_opens_or_creates_the_direct_room():
    src = _src()
    assert "async function chatWithFriend(friendId)" in src
    assert "body: { members: [friendId] }" in src
    assert "openRoomInChat(res.room_id)" in src


def test_open_room_helper_reuses_the_existing_join_path():
    src = _src()
    marker = src.index("function openRoomInChat(roomId)")
    body = src[marker:marker + 700]
    assert "refreshRoomSelect();" in body
    assert "setCurrentRoomId(roomId);" in body
    assert "joinCurrentRoom();" in body
    # The private-room card joins through the same helper instead of copying it.
    assert "openRoomInChat(roomId);" in src


def test_add_to_room_posts_a_member_and_leaves_rights_to_the_gateway():
    src = _src()
    assert "async function toggleAddToRoom(" in src
    assert "async function addRoomChoices()" in src
    assert "/members`, {" in src
    assert "body: { agent_id: friendId }" in src


def test_room_catalog_keeps_membership_fields_for_candidate_filtering():
    src = _src()
    assert "joined: !!x.joined," in src
    assert "members: x.members || 0," in src


def test_per_friend_chat_list_prefers_gateway_membership_with_a_cache_fallback():
    src = _src()
    assert "memberIds: Array.isArray(x.member_ids)" in src
    assert "const roomMembersCache = new Map();" in src
    assert "async function roomsForFriend(friendId)" in src
    assert "await membersForRoom(room)" in src
    assert 'data-chats="${esc(id)}"' in src
    assert "Chats (" in src
    # A listed chat opens through the one join path.
    assert "openRoomInChat(room.value)" in src


def test_per_friend_chat_list_sorts_by_local_activity_and_says_when_it_is_unknown():
    src = _src()
    assert "function roomActivityTs(roomId)" in src
    # Local truth only: the stored transcript for this browser.
    assert "chatHistoryKey(roomId)" in src
    assert "no local activity" in src
    # Activity is read once per room, not inside the sort comparator.
    assert "found.push({ room, ids, activity: roomActivityTs(room.value) });" in src
    assert "found.sort((a, b) => b.activity - a.activity);" in src


def test_member_lookup_failure_is_not_cached_for_the_session():
    src = _src()
    marker = src.index("async function roomMembers(roomId)")
    body = src[marker:src.index("async function membersForRoom(room)")]
    catch_at = body.index("} catch (e) {")
    assert "roomMembersCache.set" not in body[catch_at:]
    assert "error: e.message" in body[catch_at:]


def test_private_prefix_is_accepted_like_the_gateway_does():
    src = _src()
    # rooms.parse_room_id accepts ``private:`` as an alias for ``priv:``.
    assert "/^(priv|private|grp|pub):/" in src
    assert "/^(priv|private|grp):/" in src
    assert "p === 'priv' || p === 'private'" in src


def test_opening_another_panel_resets_the_displaced_caret():
    src = _src()
    marker = src.index("function removeFriendPanels(root)")
    body = src[marker:marker + 500]
    assert "querySelectorAll('[data-chats]')" in body
    assert "b.setAttribute('aria-expanded', 'false');" in body
    assert "caret.textContent = '▸';" in body


def test_rename_controls_are_labelled_for_screen_readers():
    src = _src()
    assert "'aria-label': 'Rename this room (display only)'" in src
    assert "'aria-label': 'Room display name'" in src
    assert 'aria-label="Rename this room (display only)"' in src


def test_alias_map_prunes_unknown_rooms_only_from_a_complete_catalog():
    src = _src()
    assert "for (const rid of Object.keys(next)) {" in src
    assert "if (!known.has(rid)) delete next[rid];" in src
    # The guard that keeps an empty/failed catalog from wiping every alias.
    assert "if (roomCatalog.length) {" in src


def test_alias_save_rereads_the_map_before_replacing_it():
    """A second dashboard tab's labels must survive a wholesale PATCH."""
    src = _src()
    marker = src.index("async function saveRoomAlias(roomId, label)")
    body = src[marker:src.index("// Inline editor", marker)]
    assert "const fresh = await api('/api/settings');" in body
    assert "perunchat_room_aliases" in body


def test_room_aliases_are_local_display_state():
    src = _src()
    assert "perunchat_room_aliases" in src
    assert "function roomLabel(rid)" in src
    assert "async function saveRoomAlias(roomId, label)" in src
    assert "function editRoomAlias(roomId, anchor, onDone)" in src
    # An empty value clears the entry instead of storing a blank label.
    assert "delete next[roomId];" in src
    # Every chat row and the joined header offer the inline rename control.
    assert "editRoomAlias(room.value, item, refreshSocial)" in src
    assert 'data-alias-edit' in src


def test_alias_never_masks_the_real_room_in_a_destructive_confirmation():
    src = _src()
    assert "const shown = roomAliases[roomId] ? `${roomAliases[roomId]} (${real})` : real;" in src
    assert "if (!confirm(`Remove ${mid} from ${shown}?`)) return;" in src


def test_status_dot_is_a_separate_flex_item_so_it_never_breaks_from_the_name():
    """A wrapped or long name must not push the presence dot onto its own line."""
    src = _src()
    assert 'class="row pc-friend-who"' in src
    assert 'class="pc-friend-status"' in src
    assert 'class="pc-friend-name"' in src
    css = (config.REPO_ROOT / "web" / "css" / "perun.css").read_text(encoding="utf-8")
    assert ".pc-friend-status { flex: none;" in css
    assert ".pc-friend-row { flex-wrap: wrap;" in css
    assert ".pc-friend-actions { flex: 0 0 auto;" in css
