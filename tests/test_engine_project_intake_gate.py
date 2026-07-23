"""Engine-level wiring tests for the project intake gate: an active session
must force the projectIntake tool call and skip planner/router entirely;
no active session must leave normal routing untouched; a DB error on
session lookup must fail open. See project_intake.spec.md "The gate".
"""

from unittest.mock import patch

import pytest


class _RouterReached(Exception):
    """Raised by a stubbed select_tools() to prove control reached normal
    routing — i.e. the gate did NOT short-circuit this turn."""


def _raise_router_reached(*args, **kwargs):
    raise _RouterReached("router reached")


class TestProjectIntakeGateWiring:
    def test_active_session_forces_tool_call_and_skips_planner(self, db, mock_config, dialogue_memory):
        from jarvis.reply import engine as engine_mod
        from jarvis.tools.types import ToolExecutionResult

        db.insert_intake_session()

        captured = {}

        def _fake_run_tool_with_retries(db, cfg, tool_name, tool_args, **kwargs):
            captured["tool_name"] = tool_name
            captured["tool_args"] = tool_args
            return ToolExecutionResult(success=True, reply_text="Que tipo de projeto é este?")

        with patch.object(engine_mod, "run_tool_with_retries", side_effect=_fake_run_tool_with_retries), \
             patch.object(engine_mod, "plan_query") as mock_plan, \
             patch.object(engine_mod, "select_tools") as mock_select:
            reply = engine_mod.run_reply_engine(
                db=db, cfg=mock_config, tts=None,
                text="site institucional",
                dialogue_memory=dialogue_memory,
            )

        assert captured["tool_name"] == "projectIntake"
        assert captured["tool_args"] == {"input": "site institucional"}
        assert reply == "Que tipo de projeto é este?"
        mock_plan.assert_not_called()
        mock_select.assert_not_called()

    def test_no_active_session_reaches_normal_routing(self, db, mock_config, dialogue_memory):
        from jarvis.reply import engine as engine_mod

        with patch.object(engine_mod, "select_tools", side_effect=_raise_router_reached):
            with pytest.raises(_RouterReached):
                engine_mod.run_reply_engine(
                    db=db, cfg=mock_config, tts=None,
                    text="olá",
                    dialogue_memory=dialogue_memory,
                )

    def test_db_error_on_session_lookup_fails_open(self, mock_config, dialogue_memory):
        from jarvis.reply import engine as engine_mod

        class _BrokenDB:
            def get_active_intake_session(self):
                raise RuntimeError("db unavailable")

        with patch.object(engine_mod, "select_tools", side_effect=_raise_router_reached):
            with pytest.raises(_RouterReached):
                engine_mod.run_reply_engine(
                    db=_BrokenDB(), cfg=mock_config, tts=None,
                    text="olá",
                    dialogue_memory=dialogue_memory,
                )

    def test_disabled_config_skips_gate_entirely(self, db, mock_config, dialogue_memory):
        from jarvis.reply import engine as engine_mod

        db.insert_intake_session()
        mock_config.project_intake_enabled = False

        with patch.object(engine_mod, "select_tools", side_effect=_raise_router_reached):
            with pytest.raises(_RouterReached):
                engine_mod.run_reply_engine(
                    db=db, cfg=mock_config, tts=None,
                    text="olá",
                    dialogue_memory=dialogue_memory,
                )


class TestObsidianRetryGateWiring:
    """A 'save the plan again' phrase after a completed-but-unsaved session
    must be resolved deterministically, without ever reaching the
    planner/router (which would fabricate content). See
    project_intake.spec.md "Retrying a failed Obsidian save"."""

    def _make_completed_unsaved_session(self, db):
        session_id = db.insert_intake_session()
        db.update_intake_session(
            session_id,
            project_type="other",
            status="in_progress",
            questions_json='["Qual e o nome do projeto?"]',
            current_index=1,
        )
        db.update_intake_session(session_id, answers_json='["Website da loja"]')
        db.update_intake_session(session_id, status="completed")
        return session_id

    def test_retry_phrase_forces_deterministic_resave_and_skips_planner(
        self, db, mock_config, dialogue_memory
    ):
        from jarvis.reply import engine as engine_mod
        from jarvis.tools.builtin import project_intake as pi

        self._make_completed_unsaved_session(db)

        with patch.object(pi, "write_brief_to_obsidian", return_value=True) as mock_write, \
             patch.object(engine_mod, "plan_query") as mock_plan, \
             patch.object(engine_mod, "select_tools") as mock_select:
            reply = engine_mod.run_reply_engine(
                db=db, cfg=mock_config, tts=None,
                text="tenta guardar o plano outra vez",
                dialogue_memory=dialogue_memory,
            )

        assert "obsidian" in reply.lower()
        mock_write.assert_called_once()
        sent_answers = mock_write.call_args[0][4]
        assert sent_answers == ["Website da loja"]  # the real stored answer
        mock_plan.assert_not_called()
        mock_select.assert_not_called()

    def test_unrelated_text_with_unsaved_session_reaches_normal_routing(
        self, db, mock_config, dialogue_memory
    ):
        from jarvis.reply import engine as engine_mod

        self._make_completed_unsaved_session(db)

        with patch.object(engine_mod, "select_tools", side_effect=_raise_router_reached):
            with pytest.raises(_RouterReached):
                engine_mod.run_reply_engine(
                    db=db, cfg=mock_config, tts=None,
                    text="olá",
                    dialogue_memory=dialogue_memory,
                )


class TestStartTriggerGateWiring:
    """Starting a new intake session must not depend on the chat model
    reliably choosing to invoke projectIntake — a recognised start phrase
    with no active session must force the tool call directly, bypassing
    planner/router. See project_intake.spec.md "Trigger detection"."""

    def test_start_trigger_phrase_pt_forces_tool_call_and_creates_session(
        self, db, mock_config, dialogue_memory
    ):
        from jarvis.reply import engine as engine_mod
        from jarvis.tools.builtin.project_intake import get_gated_session

        assert get_gated_session(db) is None

        with patch.object(engine_mod, "plan_query") as mock_plan, \
             patch.object(engine_mod, "select_tools") as mock_select:
            reply = engine_mod.run_reply_engine(
                db=db, cfg=mock_config, tts=None,
                text="vamos começar um novo projeto",
                dialogue_memory=dialogue_memory,
            )

        assert "tipo de projeto" in reply.lower()
        mock_plan.assert_not_called()
        mock_select.assert_not_called()
        assert get_gated_session(db) is not None

    def test_start_trigger_phrase_en_forces_tool_call(self, db, mock_config, dialogue_memory):
        from jarvis.reply import engine as engine_mod
        from jarvis.tools.builtin.project_intake import get_gated_session

        with patch.object(engine_mod, "plan_query") as mock_plan, \
             patch.object(engine_mod, "select_tools") as mock_select:
            reply = engine_mod.run_reply_engine(
                db=db, cfg=mock_config, tts=None,
                text="let's start a new project",
                dialogue_memory=dialogue_memory,
            )

        assert "tipo de projeto" in reply.lower()
        mock_plan.assert_not_called()
        mock_select.assert_not_called()
        assert get_gated_session(db) is not None

    def test_start_trigger_checked_only_after_retry_save_finds_nothing(
        self, db, mock_config, dialogue_memory
    ):
        """Text matching both the retry-save phrase and the start-trigger
        phrase must resolve via the retry-save path when a matching
        unsaved completed session exists — the retry check runs first."""
        from jarvis.reply import engine as engine_mod
        from jarvis.tools.builtin import project_intake as pi

        session_id = db.insert_intake_session()
        db.update_intake_session(
            session_id,
            project_type="other",
            status="in_progress",
            questions_json='["Qual e o nome do projeto?"]',
            current_index=1,
        )
        db.update_intake_session(session_id, answers_json='["Website da loja"]')
        db.update_intake_session(session_id, status="completed")

        ambiguous_text = "vamos comecar um novo projeto e grava o plano outra vez"
        assert pi.is_start_trigger_phrase(ambiguous_text) is True  # sanity: both would match

        with patch.object(pi, "write_brief_to_obsidian", return_value=True) as mock_write, \
             patch.object(engine_mod, "plan_query") as mock_plan, \
             patch.object(engine_mod, "select_tools") as mock_select:
            reply = engine_mod.run_reply_engine(
                db=db, cfg=mock_config, tts=None,
                text=ambiguous_text,
                dialogue_memory=dialogue_memory,
            )

        assert "obsidian" in reply.lower()
        mock_write.assert_called_once()  # retry path, not a fresh projectIntake call
        mock_plan.assert_not_called()
        mock_select.assert_not_called()

    def test_active_session_with_start_trigger_text_is_handled_by_main_gate_only(
        self, db, mock_config, dialogue_memory
    ):
        """A start-trigger phrase while a session is already active must go
        through the existing gate/restart-trigger logic (which replies
        asking to finish or abandon), not be double-handled by the new
        start-trigger check."""
        from jarvis.reply import engine as engine_mod

        db.insert_intake_session()

        with patch.object(engine_mod, "plan_query") as mock_plan, \
             patch.object(engine_mod, "select_tools") as mock_select:
            reply = engine_mod.run_reply_engine(
                db=db, cfg=mock_config, tts=None,
                text="vamos começar um novo projeto",
                dialogue_memory=dialogue_memory,
            )

        # The active-session gate forces projectIntake with the raw text,
        # which recognises this as the mid-interview restart phrase.
        assert "projeto em curso" in reply.lower()
        mock_plan.assert_not_called()
        mock_select.assert_not_called()

    def test_unrelated_project_mention_reaches_normal_routing(
        self, db, mock_config, dialogue_memory
    ):
        from jarvis.reply import engine as engine_mod

        with patch.object(engine_mod, "select_tools", side_effect=_raise_router_reached):
            with pytest.raises(_RouterReached):
                engine_mod.run_reply_engine(
                    db=db, cfg=mock_config, tts=None,
                    text="o novo projeto da câmara municipal vai custar milhões",
                    dialogue_memory=dialogue_memory,
                )
