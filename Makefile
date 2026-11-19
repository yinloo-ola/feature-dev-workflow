.PHONY: test test-plan-lint test-stack test-check-slice

test: test-plan-lint test-stack test-check-slice

test-plan-lint:
	cd skills/feature-dev-planning/scripts && python3 -m unittest -v test_plan_lint

test-stack:
	cd skills/feature-dev-planning/scripts && python3 -m unittest -v test_stack

test-check-slice:
	cd skills/slice-executor/scripts && python3 -m unittest -v test_check_slice

# Added without editing the lines above: a recipe-less rule only appends prerequisites
# to `test`, and a second .PHONY list is merged, so nothing existing is rewritten.
.PHONY: test-slice-status
test: test-slice-status

test-slice-status:
	cd skills/feature-dev-planning/scripts && python3 -m unittest -v test_slice_status
