# shellcheck shell=bash
# tickets/plugins/none/plugin.sh -- the no-op deploy plugin (T-0186): a project whose tickets never deploy through
# ticket.sh (tickets/ticket.env DEPLOY_PLUGIN=none). Every hook the core calls, each doing nothing or refusing:
# no evidence rule beyond the core's gates, no shared plans (one inbox item per ready-to-deploy ticket), no APPLY
# rule, `apply` and `deploy-check` refused. Copy this file to start a plugin: the hook list is the core's PLUGIN_HOOKS.
plugin_evidence_check() { :; }        # new-state deploy-field evidence -> why it is weak, or nothing
plugin_plan_key() { :; }              # ticket-file -> "<kind>:<plan>" for a shared plan, or nothing (the core uses ticket:<id>)
plugin_plan_void() { :; }             # plan -> why it must not run, or nothing
plugin_command_void_reason() { :; }   # command -> why a plan it names must not run, or nothing
plugin_ask_check() { :; }             # verb text ask|inbox -> why the ask is not acceptable, or nothing
plugin_verb_for() { :; }              # handoff command -> DEPLOY/PUSH/..., or nothing (RUN)
plugin_expect_line() { :; }           # verb handoff-block -> the expected last line, or nothing
plugin_ignored_valuable_re() { :; }   # an ERE of ignored files prune must keep, or nothing
plugin_deploy_kinds() { :; }          # the deploy: values this plugin handles (besides none)
plugin_deploy_check() { die "deploy-check: this project has no deploy plugin (tickets/ticket.env DEPLOY_PLUGIN=none)"; }
plugin_apply_usage() { echo "usage: apply -- this project has no deploy plugin (tickets/ticket.env DEPLOY_PLUGIN=none): nothing is applied through ticket.sh"; }
plugin_apply_prepare() { die "$(plugin_apply_usage)"; }
plugin_apply() { die "$(plugin_apply_usage)"; }
