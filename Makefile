.PHONY: run clean

run:
	@bash scripts/feishu

clean:
	@rm -rf __pycache__ .claude/settings.json sessions.json
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "清理完成"