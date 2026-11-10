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

FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.8.13 /uv /uvx /bin/

WORKDIR /code

# Dependencies come from requirements.txt, a hash-pinned export of the lockfile:
#   uv export --frozen --no-dev --no-emit-project --format requirements-txt -o requirements.txt
# --require-hashes makes the build fail unless every downloaded file matches
# the recorded hash. Re-export requirements.txt whenever dependencies change.
COPY ./requirements.txt ./
RUN uv pip install --system --no-cache --require-hashes \
    --index-url https://pypi.org/simple -r requirements.txt

COPY ./app ./app

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION}

EXPOSE 8080

CMD ["uvicorn", "app.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080"]
