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
