
# Pico Troubleshooting Guide

This guide covers common issues you may encounter while setting up and running Pico.

## 1. Pico container is not starting

**Check container status:**

```bash
docker compose ps
```

**Inspect container logs:**

```bash
docker compose logs --tail=100
```

Check for missing configuration, dependency errors, or connection failures.

## 2. Pico is not responding in Slack

Verify the following:

- The Pico container is running.
- The Slack bot is installed in the correct workspace.
- The bot has been added to the channel.
- The required Slack permissions and event subscriptions are configured.
- The configured Slack credentials are valid.

Check the container logs for Slack connection errors.

## 3. Environment configuration issues

- Confirm that the required variables are configured in `.env`.
- Compare the configuration with `.env.example`.
- Check that API credentials are valid.
- Restart the container after changing configuration.

**Important:** Never share API keys, Slack tokens, or other secrets in logs or GitHub issues.

## 4. AWS billing data is missing

Verify:

- The AWS billing export is configured.
- The expected FOCUS export files are available in S3.
- The loader has the required IAM permissions.
- The loader has completed its scheduled execution.

Check the loader logs for errors.

## 5. Still experiencing issues?

When reporting a bug, include:

- Operating system and Docker version.
- Pico version.
- Steps to reproduce the issue.
- Expected and actual behaviour.
- Relevant logs with credentials and sensitive information removed.

For security vulnerabilities, follow the repository's SECURITY.md instructions.
