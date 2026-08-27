from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from score_fourfold import ai_models
from score_fourfold.database import Database
from score_fourfold.settings_store import SettingsRepository

from .helpers import make_settings


class SettingsRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("data")
        self.database_path = self.root / f"test_settings_{self._testMethodName}.db"
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        self.database = Database(self.database_path)
        self.database.initialize()
        self.now = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        ai_models.set_prompt_overrides()

    def test_ai_prompt_settings_roundtrip_and_override_sync(self):
        self.addCleanup(ai_models.set_prompt_overrides)
        settings = make_settings(self.root, database_path=self.database_path)
        repository = SettingsRepository(self.database, settings)
        repository.initialize_from_legacy(self.now)

        snapshot = repository.public_snapshot()
        self.assertEqual(snapshot["ai_prompts"]["system_prompt"], "")
        self.assertEqual(snapshot["ai_prompts"]["plan_requirements"], "")
        self.assertEqual(snapshot["ai_prompts"]["summary_requirements"], "")
        self.assertIn(
            "爆冷分析要求",
            snapshot["ai_prompts"]["defaults"]["plan_requirements"],
        )
        self.assertIn(
            "冷门风险",
            snapshot["ai_prompts"]["defaults"]["summary_requirements"],
        )
        # 空值 = 使用内置默认。
        self.assertEqual(
            ai_models.effective_system_prompt(),
            ai_models.DEFAULT_SYSTEM_PROMPT,
        )

        repository.update_ai_prompt_settings(
            {
                "system_prompt": "自定义系统提示词",
                "plan_requirements": "自定义计划要求",
                "summary_requirements": "",
            },
            now=self.now,
        )
        snapshot = repository.public_snapshot()
        self.assertEqual(snapshot["ai_prompts"]["system_prompt"], "自定义系统提示词")
        self.assertEqual(snapshot["ai_prompts"]["plan_requirements"], "自定义计划要求")
        self.assertEqual(snapshot["ai_prompts"]["summary_requirements"], "")
        # 保存后立即同步到运行时提示词覆盖。
        self.assertEqual(ai_models.effective_system_prompt(), "自定义系统提示词")
        self.assertEqual(ai_models.prompt_overrides()["plan"], "自定义计划要求")
        self.assertEqual(ai_models.prompt_overrides()["summary"], "")

        # 重新构造仓库（模拟进程重启）会从数据库恢复覆盖。
        SettingsRepository(self.database, settings)
        self.assertEqual(ai_models.effective_system_prompt(), "自定义系统提示词")

        # 超长内容被拒绝。
        with self.assertRaises(ValueError):
            repository.update_ai_prompt_settings({"system_prompt": "x" * 8001})

    def test_initializes_all_legacy_values_once_and_encrypts_secrets(self):
        master_key = Fernet.generate_key().decode("ascii")
        settings = make_settings(
            self.root,
            database_path=self.database_path,
            mail_to="First@Example.com,second@example.com",
            smtp_username="mailer@example.com",
            smtp_auth_code="smtp-secret",
            qwen_api_key="qwen-secret",
            ai_analysis_enabled=True,
            settings_master_key=master_key,
        )
        repository = SettingsRepository(self.database, settings)

        self.assertTrue(repository.initialize_from_legacy(self.now))
        self.assertFalse(repository.initialize_from_legacy(self.now))
        snapshot = repository.public_snapshot()

        self.assertEqual(
            snapshot["recipients"],
            [
                {"email": "first@example.com", "enabled": True},
                {"email": "second@example.com", "enabled": True},
            ],
        )
        self.assertEqual(snapshot["profiles"]["crs"]["plan_count"], 3)
        self.assertEqual(snapshot["profiles"]["crs"]["min_pass_size"], 2)
        self.assertEqual(snapshot["profiles"]["crs"]["max_pass_size"], 5)
        self.assertTrue(snapshot["profiles"]["had"]["enabled"])
        self.assertFalse(snapshot["profiles"]["ttg"]["enabled"])
        self.assertEqual(snapshot["runtime"]["recommendation_times"], ["10:00", "14:00", "17:30"])
        self.assertEqual(snapshot["ai"]["active_model_config_id"], "legacy-qwen")
        self.assertTrue(snapshot["ai"]["models"][0]["api_key_configured"])
        self.assertTrue(snapshot["secret_storage_ready"])

        with self.database.connect() as connection:
            model = connection.execute(
                "SELECT api_key_ciphertext, api_key_env FROM ai_model_configs"
            ).fetchone()
            mail = connection.execute(
                "SELECT smtp_auth_ciphertext, smtp_auth_env FROM notification_settings"
            ).fetchone()
        self.assertNotEqual(model["api_key_ciphertext"], "qwen-secret")
        self.assertEqual(model["api_key_env"], "")
        self.assertNotEqual(mail["smtp_auth_ciphertext"], "smtp-secret")
        self.assertEqual(mail["smtp_auth_env"], "")
        self.assertEqual(
            repository.resolve_secret(model["api_key_ciphertext"], model["api_key_env"]),
            "qwen-secret",
        )

    def test_without_master_key_keeps_legacy_secrets_in_environment_only(self):
        settings = make_settings(
            self.root,
            database_path=self.database_path,
            smtp_auth_code="legacy-smtp",
            qwen_api_key="legacy-qwen",
            settings_master_key="",
        )
        repository = SettingsRepository(self.database, settings)
        repository.initialize_from_legacy(self.now)
        with self.database.connect() as connection:
            model = connection.execute(
                "SELECT api_key_ciphertext, api_key_env FROM ai_model_configs"
            ).fetchone()
            mail = connection.execute(
                "SELECT smtp_auth_ciphertext, smtp_auth_env FROM notification_settings"
            ).fetchone()
        self.assertEqual(model["api_key_ciphertext"], "")
        self.assertEqual(model["api_key_env"], "QWEN_API_KEY")
        self.assertEqual(mail["smtp_auth_ciphertext"], "")
        self.assertEqual(mail["smtp_auth_env"], "SMTP_AUTH_CODE")
        with patch.dict("os.environ", {"QWEN_API_KEY": "legacy-qwen"}):
            self.assertEqual(repository.resolve_secret("", "QWEN_API_KEY"), "legacy-qwen")
        self.assertFalse(repository.public_snapshot()["secret_storage_ready"])

    def test_partial_initialization_is_rejected_without_overwrite(self):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO email_recipients (email, enabled, position) VALUES ('keep@example.com', 1, 0)"
            )
        repository = SettingsRepository(
            self.database,
            make_settings(self.root, database_path=self.database_path),
        )
        with self.assertRaises(RuntimeError):
            repository.initialize_from_legacy(self.now)
        with self.database.connect() as connection:
            meta = connection.execute("SELECT * FROM settings_meta").fetchall()
            recipients = connection.execute("SELECT email FROM email_recipients").fetchall()
        self.assertEqual(meta, [])
        self.assertEqual([row["email"] for row in recipients], ["keep@example.com"])

    def test_model_is_activated_only_after_required_web_search_test_passes(self):
        master_key = Fernet.generate_key().decode("ascii")
        settings = make_settings(
            self.root,
            database_path=self.database_path,
            qwen_api_key="legacy-key",
            settings_master_key=master_key,
        )
        repository = SettingsRepository(self.database, settings)
        repository.initialize_from_legacy(self.now)
        new_id = repository.save_model_config(
            provider="qwen",
            display_name="备用千问",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/responses",
            model_name="qwen3.7-max",
            api_key="new-key",
            now=self.now,
        )
        called: list[str] = []

        def passing(runtime, _timeout):
            called.append(runtime.config_id)
            return "测试通过"

        success, detail = repository.test_and_activate_model(
            new_id,
            tester=passing,
            now=self.now,
        )
        self.assertTrue(success)
        self.assertEqual(detail, "测试通过")
        self.assertEqual(called, [new_id])
        snapshot = repository.public_snapshot()
        self.assertEqual(snapshot["ai"]["active_model_config_id"], new_id)
        self.assertEqual(
            [model["last_test_status"] for model in snapshot["ai"]["models"] if model["id"] == new_id],
            ["passed"],
        )

    def test_unlimited_analysis_timeout_still_bounds_synchronous_probe(self):
        master_key = Fernet.generate_key().decode("ascii")
        settings = make_settings(
            self.root,
            database_path=self.database_path,
            qwen_api_key="legacy-key",
            settings_master_key=master_key,
            ai_http_timeout_seconds=0,
        )
        repository = SettingsRepository(self.database, settings)
        repository.initialize_from_legacy(self.now)
        # 后台 AI 分析超时为 0（不限制）。
        self.assertEqual(repository.effective_settings().ai_http_timeout_seconds, 0)
        # 同步模型测试走网页请求（OpenResty 读超时 660 秒），必须保持 600 秒上限。
        received: list[int] = []

        def passing(runtime, timeout):
            received.append(timeout)
            return "测试通过"

        repository.test_and_activate_model(
            "legacy-qwen",
            tester=passing,
            now=self.now,
        )
        self.assertEqual(received, [600])

    def test_failed_or_unsupported_model_test_keeps_previous_active_model(self):
        master_key = Fernet.generate_key().decode("ascii")
        settings = make_settings(
            self.root,
            database_path=self.database_path,
            qwen_api_key="legacy-key",
            settings_master_key=master_key,
        )
        repository = SettingsRepository(self.database, settings)
        repository.initialize_from_legacy(self.now)
        zhipu_id = repository.save_model_config(
            provider="zhipu",
            display_name="智谱 GLM",
            base_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
            model_name="glm-4.5",
            api_key="glm-key",
            now=self.now,
        )
        success, detail = repository.test_and_activate_model(
            zhipu_id,
            tester=lambda *_: self.fail("unsupported provider must fail before network"),
            now=self.now,
        )
        self.assertFalse(success)
        self.assertIn("强制联网搜索", detail)
        snapshot = repository.public_snapshot()
        self.assertEqual(snapshot["ai"]["active_model_config_id"], "legacy-qwen")
        failed = next(model for model in snapshot["ai"]["models"] if model["id"] == zhipu_id)
        self.assertEqual(failed["last_test_status"], "failed")

    def test_set_active_model_config_switches_only_passed_models(self):
        master_key = Fernet.generate_key().decode("ascii")
        settings = make_settings(
            self.root,
            database_path=self.database_path,
            qwen_api_key="legacy-key",
            settings_master_key=master_key,
        )
        repository = SettingsRepository(self.database, settings)
        repository.initialize_from_legacy(self.now)
        snapshot = repository.public_snapshot()
        self.assertEqual(snapshot["ai"]["active_model_config_id"], "legacy-qwen")

        # Build a second qwen model and verify activation requires a passed test.
        second_id = repository.save_model_config(
            provider="qwen",
            display_name="备用千问",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/responses",
            model_name="qwen3.7-max",
            api_key="new-key",
            now=self.now,
        )
        with self.assertRaises(ValueError) as ctx:
            repository.set_active_model_config(second_id, now=self.now)
        self.assertIn("尚未通过测试", str(ctx.exception))

        def passing(runtime, _timeout):
            return "OK"

        repository.test_and_activate_model(second_id, tester=passing, now=self.now)
        self.assertEqual(repository.public_snapshot()["ai"]["active_model_config_id"], second_id)

        # Switching back to the legacy model only requires it to remain passed.
        repository.test_and_activate_model("legacy-qwen", tester=passing, now=self.now)
        repository.set_active_model_config(second_id, now=self.now)
        self.assertEqual(repository.public_snapshot()["ai"]["active_model_config_id"], second_id)

        # Switching to a non-existent or untested model must fail safely.
        with self.assertRaises(ValueError):
            repository.set_active_model_config("ghost-id", now=self.now)

        # Adding an unsupported provider must not become activatable even after a test pass.
        zhipu_id = repository.save_model_config(
            provider="zhipu",
            display_name="智谱 GLM",
            base_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
            model_name="glm-4.5",
            api_key="glm-key",
            now=self.now,
        )
        with patch.object(repository, "test_and_activate_model") as fake_activate:
            fake_activate.return_value = (True, "测试通过")
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE ai_model_configs SET last_test_status = 'passed' WHERE model_config_id = ?",
                    (zhipu_id,),
                )
        with self.assertRaises(ValueError) as ctx:
            repository.set_active_model_config(zhipu_id, now=self.now)
        self.assertIn("联网搜索", str(ctx.exception))

    def test_updates_business_settings_and_exposes_effective_runtime(self):
        settings = make_settings(self.root, database_path=self.database_path)
        repository = SettingsRepository(self.database, settings)
        repository.initialize_from_legacy(self.now)
        repository.update_recommendation_profiles(
            {
                "crs": {"enabled": True, "min_pass_size": 2, "max_pass_size": 3, "plan_count": 2},
                "had": {"enabled": False, "min_pass_size": 4, "max_pass_size": 6, "plan_count": 1},
                "ttg": {"enabled": True, "min_pass_size": 2, "max_pass_size": 4, "plan_count": 2},
            },
            now=self.now,
        )
        repository.update_recipients(["one@example.com", "two@example.com"])
        repository.update_runtime_settings(
            {
                "recommendation_times": ["09:30", "14:30"],
                "recommendation_first_mail_time": "15:00",
                "recommendation_latest_start": "17:30",
                "recommendation_deadline": "18:00",
                "recommendation_send_buffer_minutes": 10,
                "poll_interval_seconds": 600,
                "result_check_delay_minutes": 180,
                "send_no_recommendation": False,
            },
            now=self.now,
        )
        effective = repository.effective_settings()
        snapshot = repository.public_snapshot()
        self.assertEqual(effective.mail_to, "one@example.com,two@example.com")
        self.assertEqual([value.strftime("%H:%M") for value in effective.recommendation_times], ["09:30", "14:30"])
        self.assertEqual(effective.poll_interval_seconds, 600)
        self.assertEqual(effective.result_check_delay_minutes, 180)
        self.assertFalse(effective.send_no_recommendation)
        self.assertEqual(snapshot["profiles"]["crs"]["plan_count"], 2)
        self.assertTrue(snapshot["profiles"]["ttg"]["enabled"])

    def test_rejects_invalid_runtime_order_without_changing_existing_values(self):
        settings = make_settings(self.root, database_path=self.database_path)
        repository = SettingsRepository(self.database, settings)
        repository.initialize_from_legacy(self.now)
        before = repository.public_snapshot()["runtime"]
        with self.assertRaises(ValueError):
            repository.update_runtime_settings(
                {
                    "recommendation_times": ["17:50"],
                    "recommendation_first_mail_time": "15:00",
                    "recommendation_latest_start": "17:45",
                    "recommendation_deadline": "18:00",
                    "recommendation_send_buffer_minutes": 10,
                    "poll_interval_seconds": 600,
                    "result_check_delay_minutes": 180,
                    "send_no_recommendation": True,
                }
            )
        self.assertEqual(repository.public_snapshot()["runtime"], before)

    def test_legacy_deepseek_endpoint_is_migrated_to_responses(self):
        master_key = Fernet.generate_key().decode("ascii")
        settings = make_settings(
            self.root,
            database_path=self.database_path,
            qwen_api_key="legacy-key",
            settings_master_key=master_key,
        )
        repository = SettingsRepository(self.database, settings)
        repository.initialize_from_legacy(self.now)
        deepseek_id = repository.save_model_config(
            provider="deepseek",
            display_name="DeepSeek",
            base_url="https://api.deepseek.com/chat/completions",
            model_name="deepseek-v4-flash",
            api_key="ds-key",
            now=self.now,
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE ai_model_configs
                SET last_test_status = 'passed', last_test_detail = '旧测试结果'
                WHERE model_config_id = ?
                """,
                (deepseek_id,),
            )

        snapshot = repository.public_snapshot()
        migrated = next(model for model in snapshot["ai"]["models"] if model["id"] == deepseek_id)
        self.assertEqual(migrated["base_url"], "https://api.deepseek.com/responses")
        # 迁移必须重置旧的测试结果，避免基于旧端点的"已通过"被误信。
        self.assertEqual(migrated["last_test_status"], "untested")

        # 迁移是幂等的，重复调用不会重复修改数据库。
        again = repository.public_snapshot()
        migrated_again = next(model for model in again["ai"]["models"] if model["id"] == deepseek_id)
        self.assertEqual(migrated_again["base_url"], "https://api.deepseek.com/responses")


if __name__ == "__main__":
    unittest.main()
