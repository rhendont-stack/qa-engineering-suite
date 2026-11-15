# Notes service: build and release

The build and release configuration of the notes service. The CI workflow tests every change, and the deploy job publishes a
release when a change reaches `main`.

## Layout

- `.github/workflows/ci.yml`: tests every push and pull request, and deploys from `main`.
- `.github/workflows/pr-preview.yml`: builds a preview image for each pull request.
- `Dockerfile`: the service image.

## Deploy

1. Merge the pull request into `main`. The CI workflow runs the tests.
2. Build the image: `docker build --tag registry.example.com/notes:1.4.0 .`
3. Push it: `docker push registry.example.com/notes:1.4.0`
4. The deploy job posts the release to `https://deploy.example.com/api/releases`, using the `DEPLOY_TOKEN` repository secret.
5. Open `https://notes.example.com/health` and check that it answers `ok`.
6. Announce the release in the team channel.

## Configuration

The service reads its settings from environment variables: `NOTES_DATABASE_URL`, `NOTES_PORT` and `NOTES_LOG_LEVEL`.
