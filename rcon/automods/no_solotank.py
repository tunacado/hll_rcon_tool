"""
Enforces the "An armor squad must have at least two members" rule
"""

import logging
import pickle
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Literal

import redis

from rcon.automods.get_team_count import get_team_count
from rcon.automods.is_time import is_time
from rcon.automods.models import (
    ActionMethod,
    NoSoloTanker,
    PunishDetails,
    PunishPlayer,
    PunishStepState,
    PunitionsToApply,
    WatchStatus,
)
from rcon.automods.num_or_inf import num_or_inf
from rcon.types import GameStateType
from rcon.user_config.auto_mod_solo_tank import AutoModNoSoloTankUserConfig

SOLO_TANK_RESET_SECS = 120
AUTOMOD_USERNAME = "AutoMod_NoSoloTank"


class NoSoloTankAutomod:
    """
    Imported from rcon/automods/automod.py
    """

    logger: logging.Logger
    red: redis.StrictRedis
    config: AutoModNoSoloTankUserConfig

    def __init__(
        self, config: AutoModNoSoloTankUserConfig, red: redis.StrictRedis or None
    ):
        self.logger = logging.getLogger(__name__)
        self.red = red
        self.config = config

    def enabled(self):
        """
        Global on/off switch
        """
        return self.config.enabled

    @contextmanager
    def watch_state(self, team: str, squad_name: str):
        """
        Observe and actualize the current moderation step
        """
        redis_key = f"no_solo_tank{team.lower()}{str(squad_name).lower()}"
        watch_status = self.red.get(redis_key)
        if watch_status:
            watch_status = pickle.loads(watch_status)
        else:  # No punishments so far, starting a fresh one
            watch_status = WatchStatus()

        try:
            yield watch_status
        except NoSoloTanker:
            self.logger.debug(
                "Squad %s - %s no solotank violation, clearing state", team, squad_name
            )
            self.red.delete(redis_key)
        else:
            self.red.setex(redis_key, SOLO_TANK_RESET_SECS, pickle.dumps(watch_status))

    def get_message(
        self, watch_status: WatchStatus, aplayer: PunishPlayer, method: ActionMethod
    ):
        """
        Construct the message sent to the player
        according to the actual moderation step
        """
        data = {}

        if method == ActionMethod.MESSAGE:
            data["received_warnings"] = len(watch_status.warned.get(aplayer.name))
            data["max_warnings"] = self.config.number_of_warnings
            data["next_check_seconds"] = self.config.warning_interval_seconds

        if method == ActionMethod.PUNISH:
            data["received_punishes"] = len(watch_status.punished.get(aplayer.name))
            data["max_punishes"] = self.config.number_of_punishments
            data["next_check_seconds"] = self.config.punish_interval_seconds

        if method == ActionMethod.KICK:
            data["kick_grace_period"] = self.config.kick_grace_period_seconds

        data["player_name"] = aplayer.name
        data["squad_name"] = aplayer.squad

        base_message = {
            ActionMethod.MESSAGE: self.config.warning_message,
            ActionMethod.PUNISH: self.config.punish_message,
            ActionMethod.KICK: self.config.kick_message,
        }

        message = base_message[method]
        try:
            return message.format(**data)
        except KeyError:
            self.logger.warning(
                "The automod message of %s (%s) contains an invalid key",
                repr(method),
                message,
            )
            return message

    def player_punish_failed(self, aplayer):
        """
        A dead/unspawned player can't be punished
        Resets the timer from the last unsuccessful punish.
        """
        with self.watch_state(aplayer.team, aplayer.squad) as watch_status:
            try:
                if punishes := watch_status.punished.get(aplayer.name):
                    del punishes[-1]
            except Exception:
                self.logger.exception("tried to cleanup punished time but failed")

    def punitions_to_apply(
        self,
        team_view,
        squad_name: str,
        team: Literal["axis", "allies"],
        squad: dict,
        game_state: GameStateType,
    ) -> PunitionsToApply:
        """
        Observe all squads/players
        Find the ones who trespass rules
        Sends them to their next moderation step
        """
        self.logger.debug("Squad %s %s", squad_name, squad)
        punitions_to_apply = PunitionsToApply()

        server_player_count = get_team_count(team_view, "allies") + get_team_count(
            team_view, "axis"
        )

        # dont_do_anything_below_this_number_of_players
        if (
            server_player_count
            < self.config.dont_do_anything_below_this_number_of_players
        ):
            self.logger.debug("Server below min player count : disabling")
            return punitions_to_apply

        if not squad_name:
            self.logger.debug(
                "Skipping None or empty squad - (%s) %s", team, squad_name
            )
            return punitions_to_apply

        with self.watch_state(team, squad_name) as watch_status:

            # if squad_name is None or squad is None:
            #     raise NoSoloTanker()

            if squad_name == "Commander":
                self.logger.debug("Skipping commander")
                raise NoSoloTanker()

            if squad["type"] != "armor":
                self.logger.debug("Squad type is not armor - (%s) %s", team, squad_name)
                raise NoSoloTanker()

            if len(squad["players"]) > 1:
                self.logger.debug(
                    "Armor squad with more than one member - (%s) %s", team, squad_name
                )
                raise NoSoloTanker()

            self.logger.debug("Solo tank squad - (%s) %s", team, squad_name)

            author = AUTOMOD_USERNAME + ("-DryRun" if self.config.dry_run else "")

            for player in squad["players"]:
                profile = player.get("profile", {})
                aplayer = PunishPlayer(
                    player_id=player["player_id"],
                    name=player["name"],
                    squad=squad_name,
                    team=team,
                    flags=profile.get("flags") if profile else [],
                    role=player.get("role"),
                    lvl=int(player.get("level")),
                    details=PunishDetails(
                        author=author,
                        dry_run=self.config.dry_run,
                        discord_audit_url=self.config.discord_webhook_url,
                    ),
                )

                # Note
                state = self.should_note_player(watch_status, squad_name, aplayer)

                if state == PunishStepState.APPLY:
                    punitions_to_apply.add_squad_state(team, squad_name, squad)

                if not state in [
                    PunishStepState.DISABLED,
                    PunishStepState.GO_TO_NEXT_STEP,
                ]:
                    continue

                # Warning
                state = self.should_warn_player(watch_status, squad_name, aplayer)

                if state == PunishStepState.APPLY:
                    aplayer.details.message = self.get_message(
                        watch_status, aplayer, ActionMethod.MESSAGE
                    )
                    punitions_to_apply.warning.append(aplayer)
                    punitions_to_apply.add_squad_state(team, squad_name, squad)

                if state == PunishStepState.WAIT:
                    # only here to make the tests pass, otherwise useless
                    punitions_to_apply.add_squad_state(team, squad_name, squad)

                if not state in [
                    PunishStepState.DISABLED,
                    PunishStepState.GO_TO_NEXT_STEP,
                ]:
                    continue

                # Punish
                state = self.should_punish_player(
                    watch_status, team_view, squad_name, squad, aplayer
                )

                if state == PunishStepState.APPLY:
                    aplayer.details.message = self.get_message(
                        watch_status, aplayer, ActionMethod.PUNISH
                    )
                    punitions_to_apply.punish.append(aplayer)
                    punitions_to_apply.add_squad_state(team, squad_name, squad)

                if not state in [
                    PunishStepState.DISABLED,
                    PunishStepState.GO_TO_NEXT_STEP,
                ]:
                    continue

                # Kick
                state = self.should_kick_player(
                    watch_status, team_view, squad_name, squad, aplayer
                )

                if state == PunishStepState.APPLY:
                    aplayer.details.message = self.get_message(
                        watch_status, aplayer, ActionMethod.KICK
                    )
                    punitions_to_apply.kick.append(aplayer)
                    punitions_to_apply.add_squad_state(team, squad_name, squad)

                if state not in [
                    PunishStepState.DISABLED,
                    PunishStepState.GO_TO_NEXT_STEP,
                ]:
                    continue

        return punitions_to_apply

    def should_note_player(
        self, watch_status: WatchStatus, squad_name: str, aplayer: PunishPlayer
    ):
        """
        Prepare moderation
        This player is trespassing a rule
        For now, we just wait, in case the server returned obsolete data.
        or for the trespassing to disappear rapidly.
        """
        # number_of_notes
        if self.config.number_of_notes == 0:
            self.logger.debug("Notes are disabled. number_of_notes is set to 0")
            return PunishStepState.DISABLED

        # whitelist_flags
        if any(
            flag_entry.flag in self.config.whitelist_flags
            for flag_entry in aplayer.flags
        ):
            self.logger.debug("%s is immune to notes", aplayer.short_repr())
            return PunishStepState.IMMUNED

        # (not applicable)
        # immune_roles

        # immune_player_level
        if aplayer.lvl <= self.config.immune_player_level:
            self.logger.debug("%s is immune to notes", aplayer.short_repr())
            return PunishStepState.IMMUNED

        notes = watch_status.noted.setdefault(aplayer.name, [])

        # notes_interval_seconds
        if not is_time(notes, self.config.notes_interval_seconds):
            self.logger.debug(
                "Waiting to note: %s in %s", aplayer.short_repr(), squad_name
            )
            return PunishStepState.WAIT

        # number_of_notes
        if len(notes) < self.config.number_of_notes:
            self.logger.info(
                "%s Will be noted (%s/%s)",
                aplayer.short_repr(),
                len(notes),
                num_or_inf(self.config.number_of_notes),
            )
            notes.append(datetime.now())
            return PunishStepState.APPLY

        self.logger.info(
            "%s Max notes reached (%s/%s). Moving on to warn.",
            aplayer.short_repr(),
            len(notes),
            self.config.number_of_notes,
        )
        return PunishStepState.GO_TO_NEXT_STEP

    def should_warn_player(
        self, watch_status: WatchStatus, squad_name: str, aplayer: PunishPlayer
    ):
        """
        Send a message to trespassers
        telling them they must follow the rules
        before being punished and kicked
        """
        # number_of_warnings
        if self.config.number_of_warnings == 0:
            self.logger.debug("Warnings are disabled. number_of_warning is set to 0")
            return PunishStepState.DISABLED

        # whitelist_flags
        if any(
            flag_entry.flag in self.config.whitelist_flags
            for flag_entry in aplayer.flags
        ):
            self.logger.debug("%s is immune to warnings", aplayer.short_repr())
            return PunishStepState.IMMUNED

        # (not applicable)
        # immune_roles

        # immune_player_level
        if aplayer.lvl <= self.config.immune_player_level:
            self.logger.debug("%s is immune to warnings", aplayer.short_repr())
            return PunishStepState.IMMUNED

        warnings = watch_status.warned.setdefault(aplayer.name, [])

        # warning_interval_seconds
        if not is_time(warnings, self.config.warning_interval_seconds):
            self.logger.debug(
                "Waiting to warn: %s in %s", aplayer.short_repr(), squad_name
            )
            return PunishStepState.WAIT

        # number_of_warnings
        if (
            len(warnings) < self.config.number_of_warnings
            or self.config.number_of_warnings == -1
        ):
            self.logger.info(
                "%s Will be warned (%s/%s)",
                aplayer.short_repr(),
                len(warnings),
                num_or_inf(self.config.number_of_warnings),
            )
            warnings.append(datetime.now())
            return PunishStepState.APPLY

        self.logger.info(
            "%s Max warnings reached (%s/%s). Moving on to punish.",
            aplayer.short_repr(),
            len(warnings),
            self.config.number_of_warnings,
        )
        return PunishStepState.GO_TO_NEXT_STEP

    def should_punish_player(
        self,
        watch_status: WatchStatus,
        team_view,
        squad_name: str,
        squad,
        aplayer: PunishPlayer,
    ):
        """
        Punish (kill) trespassers
        telling them they must follow the rules
        before being kicked
        """
        # number_of_punishments
        if self.config.number_of_punishments == 0:
            self.logger.debug("Punish is disabled")
            return PunishStepState.DISABLED

        # whitelist_flags
        if any(
            flag_entry.flag in self.config.whitelist_flags
            for flag_entry in aplayer.flags
        ):
            self.logger.debug("%s is immune to punishment", aplayer.short_repr())
            return PunishStepState.IMMUNED

        # (not applicable)
        # immune_roles

        # immune_player_level
        if aplayer.lvl <= self.config.immune_player_level:
            self.logger.debug("%s is immune to punishment", aplayer.short_repr())
            return PunishStepState.IMMUNED

        # (not applicable)
        # min_squad_players_for_punish

        # min_server_players_for_punish
        if (
            get_team_count(team_view, "allies") + get_team_count(team_view, "axis")
        ) < self.config.min_server_players_for_punish:
            self.logger.debug("Server below min player count for punish")
            return PunishStepState.WAIT

        punishes = watch_status.punished.setdefault(aplayer.name, [])

        # punish_interval_seconds
        if not is_time(punishes, self.config.punish_interval_seconds):
            self.logger.debug("Waiting to punish %s", squad_name)
            return PunishStepState.WAIT

        # number_of_punishments
        if (
            len(punishes) < self.config.number_of_punishments
            or self.config.number_of_punishments == -1
        ):
            self.logger.info(
                "%s Will be punished (%s/%s)",
                aplayer.short_repr(),
                len(punishes),
                num_or_inf(self.config.number_of_punishments),
            )
            punishes.append(datetime.now())
            return PunishStepState.APPLY

        self.logger.info(
            "%s Max punish reached (%s/%s)",
            aplayer.short_repr(),
            len(punishes),
            self.config.number_of_punishments,
        )
        return PunishStepState.GO_TO_NEXT_STEP

    def should_kick_player(
        self,
        watch_status: WatchStatus,
        team_view,
        squad_name: str,
        squad,
        aplayer: PunishPlayer,
    ):
        """
        Kick (disconnect) trespassers
        telling them they must follow the rules
        """
        # kick_after_max_punish
        if not self.config.kick_after_max_punish:
            self.logger.debug("Kick is disabled")
            return PunishStepState.DISABLED

        # whitelist_flags
        if any(
            flag_entry.flag in self.config.whitelist_flags
            for flag_entry in aplayer.flags
        ):
            self.logger.debug("%s is immune to kick", aplayer.short_repr())
            return PunishStepState.IMMUNED

        # (not applicable)
        # immune_roles

        # immune_player_level
        if aplayer.lvl <= self.config.immune_player_level:
            self.logger.debug("%s is immune to kick", aplayer.short_repr())
            return PunishStepState.IMMUNED

        # (not applicable)
        # min_squad_players_for_kick

        # min_server_players_for_kick
        if (
            get_team_count(team_view, "allies") + get_team_count(team_view, "axis")
        ) < self.config.min_server_players_for_kick:
            self.logger.debug("Server below min player count for punish")
            return PunishStepState.WAIT

        try:
            last_time = watch_status.punished.get(aplayer.name, [])[-1]
        except IndexError:
            self.logger.error("Trying to kick player without prior punishes")
            return PunishStepState.DISABLED

        # kick_grace_period_seconds
        if datetime.now() - last_time < timedelta(
            seconds=self.config.kick_grace_period_seconds
        ):
            return PunishStepState.WAIT

        return PunishStepState.APPLY
