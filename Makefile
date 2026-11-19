.PHONY: test test-plan-lint test-stack test-check-slice

test: test-plan-lint test-stack test-check-slice

test-plan-lint:
	cd skills/feature-dev-planning/scripts && python3 -m unittest -v test_plan_lint

test-stack:
	cd skills/feature-dev-planning/scripts && python3 -m unittest -v test_stack

test-check-slice:
	cd skills/slice-executor/scripts && python3 -m unittest -v test_check_slice
