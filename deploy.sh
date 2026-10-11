#!/usr/bin/env bash
# Cloud Run Jobs + Cloud Scheduler（1時間ごと）にデプロイする.
#   使い方:  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... ./deploy.sh
#   2回目以降（config.yaml / watchlist.yaml 変更時）も同じコマンドで再デプロイできます。
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-ur-scraper-510501}"
REGION="${REGION:-asia-northeast1}"
JOB="${JOB:-ur-vacancy-notifier}"
BUCKET="${BUCKET:-${PROJECT_ID}-ur-vacancy-state}"
SCHEDULE="${SCHEDULE:-0 * * * *}"   # 毎時0分（JST）
RUN_SA="ur-watcher@${PROJECT_ID}.iam.gserviceaccount.com"
SCHED_SA="ur-watcher-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID" >/dev/null

echo "== API 有効化"
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com storage.googleapis.com

echo "== 状態保存用バケット"
gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1 || \
  gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" --uniform-bucket-level-access

echo "== サービスアカウント"
gcloud iam service-accounts describe "$RUN_SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create ur-watcher --display-name="UR vacancy watcher"
gcloud iam service-accounts describe "$SCHED_SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create ur-watcher-scheduler --display-name="UR vacancy watcher scheduler"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${RUN_SA}" --role="roles/storage.objectAdmin" >/dev/null

echo "== Discord Webhook を Secret Manager に保存"
if [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
  gcloud secrets describe discord-webhook >/dev/null 2>&1 || \
    gcloud secrets create discord-webhook --replication-policy=automatic
  printf '%s' "$DISCORD_WEBHOOK_URL" | gcloud secrets versions add discord-webhook --data-file=-
fi
gcloud secrets add-iam-policy-binding discord-webhook \
  --member="serviceAccount:${RUN_SA}" --role="roles/secretmanager.secretAccessor" >/dev/null

echo "== Cloud Run Job をビルド＆デプロイ"
gcloud run jobs deploy "$JOB" \
  --source . \
  --region "$REGION" \
  --service-account "$RUN_SA" \
  --set-env-vars "STATE_BUCKET=${BUCKET}" \
  --set-secrets "DISCORD_WEBHOOK_URL=discord-webhook:latest" \
  --task-timeout 15m \
  --max-retries 0 \
  --memory 512Mi

gcloud run jobs add-iam-policy-binding "$JOB" --region "$REGION" \
  --member="serviceAccount:${SCHED_SA}" --role="roles/run.invoker" >/dev/null

echo "== Cloud Scheduler（${SCHEDULE} / Asia/Tokyo）"
URI="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run"
if gcloud scheduler jobs describe "${JOB}-hourly" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${JOB}-hourly" --location "$REGION" \
    --schedule "$SCHEDULE" --time-zone "Asia/Tokyo" --uri "$URI" --http-method POST \
    --oauth-service-account-email "$SCHED_SA"
else
  gcloud scheduler jobs create http "${JOB}-hourly" --location "$REGION" \
    --schedule "$SCHEDULE" --time-zone "Asia/Tokyo" --uri "$URI" --http-method POST \
    --oauth-service-account-email "$SCHED_SA"
fi

echo "== 完了。手動で1回実行するには:"
echo "   gcloud run jobs execute ${JOB} --region ${REGION} --wait"
