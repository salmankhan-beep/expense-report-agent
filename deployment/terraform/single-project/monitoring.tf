# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Business metrics and alerting built from the agent's structured logs.
#
# The agent writes one JSON line per event to stdout (app/observability.py).
# Agent Runtime stores those in the reasoning_engine_stdout log as jsonPayload
# entries on the aiplatform.googleapis.com/ReasoningEngine resource, which the
# filters below match.

locals {
  agent_log_filter = join(" AND ", [
    "resource.type=\"aiplatform.googleapis.com/ReasoningEngine\"",
    "logName=\"projects/${var.project_id}/logs/aiplatform.googleapis.com%2Freasoning_engine_stdout\"",
  ])
}

# Decisions by outcome (approved/rejected) and who decided (auto/manager).
resource "google_logging_metric" "expense_decisions" {
  project = var.project_id
  name    = "${var.project_name}_expense_decisions"
  filter  = "${local.agent_log_filter} AND jsonPayload.event=\"expense_decided\""

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Expense decisions"

    labels {
      key         = "decision"
      value_type  = "STRING"
      description = "approved or rejected"
    }
    labels {
      key         = "decided_by"
      value_type  = "STRING"
      description = "auto or manager"
    }
  }

  label_extractors = {
    "decision"   = "EXTRACT(jsonPayload.decision)"
    "decided_by" = "EXTRACT(jsonPayload.decided_by)"
  }

  depends_on = [google_project_service.services]
}

# Routing outcomes: how many claims are auto-approved vs sent to review.
resource "google_logging_metric" "expense_routing" {
  project = var.project_id
  name    = "${var.project_name}_expense_routing"
  filter  = "${local.agent_log_filter} AND jsonPayload.event=\"expense_routed\""

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Expense routing outcomes"

    labels {
      key         = "route"
      value_type  = "STRING"
      description = "AUTO_APPROVE or NEEDS_REVIEW"
    }
  }

  label_extractors = {
    "route" = "EXTRACT(jsonPayload.route)"
  }

  depends_on = [google_project_service.services]
}

# Review alerts raised by the review agent's emit_expense_alert tool.
resource "google_logging_metric" "expense_review_alerts" {
  project = var.project_id
  name    = "${var.project_name}_expense_review_alerts"
  filter  = "${local.agent_log_filter} AND jsonPayload.alert_type=\"expense_review\""

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Expense review alerts"
  }

  depends_on = [google_project_service.services]
}

resource "google_monitoring_notification_channel" "finance_email" {
  count        = var.alert_notification_email == "" ? 0 : 1
  project      = var.project_id
  display_name = "${var.project_name} finance alerts"
  type         = "email"

  labels = {
    email_address = var.alert_notification_email
  }

  depends_on = [google_project_service.services]
}

# Fires when an unusual number of expenses need review in a short window,
# e.g. a burst of duplicate or over-limit claims.
resource "google_monitoring_alert_policy" "review_alert_spike" {
  project      = var.project_id
  display_name = "${var.project_name}: spike in expense review alerts"
  combiner     = "OR"

  conditions {
    display_name = "More than ${var.review_alert_threshold} review alerts in 10 minutes"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.expense_review_alerts.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.review_alert_threshold
      duration        = "0s"

      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Many expenses were flagged for review in the last 10 minutes. Open the manager dashboard to work the queue, and check Cloud Trace (`expense.route` spans) for the routing reasons."
  }

  notification_channels = google_monitoring_notification_channel.finance_email[*].id
}
