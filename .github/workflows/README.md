# GitHub Actions CI/CD

This directory contains GitHub Actions workflows for automated testing, code quality checks, and publishing.

## Workflows

### 1. `test.yml` - Automated Testing

**Triggers**: Push and Pull Requests to main/master/develop branches

**Jobs**:

- **Lint**: Basic linting with ruff and mypy
- **Mock Tests**: Fast unit tests with mock objects (Python 3.10, 3.11, 3.12)
  - Runs `tests/test_pagepool_simple.py`
  - Generates coverage report
  - Uploads to Codecov
- **Integration Tests**: Full integration tests with Playwright (Python 3.10, 3.11, 3.12)
  - Runs `tests/test_pagepool.py`
  - Installs Playwright with system dependencies
  - May fail due to system dependencies (set to continue-on-error)
- **Demo Validation**: Verifies demo script runs successfully

**Status Badge**:
```markdown
![Tests](https://github.com/YOUR_USERNAME/pwutil/actions/workflows/test.yml/badge.svg)
```

### 2. `code-quality.yml` - Code Quality

**Triggers**: Push and Pull Requests to main/master/develop branches

**Jobs**:

- **Code Quality Checks**:
  - Black (code formatting)
  - isort (import sorting)
  - Ruff (fast Python linting)
  - mypy (type checking)
  - Bandit (security vulnerability scanning)
  - Safety (dependency vulnerability checking)

- **Complexity Analysis**:
  - Cyclomatic complexity (radon cc)
  - Maintainability index (radon mi)

**Status Badge**:
```markdown
![Code Quality](https://github.com/YOUR_USERNAME/pwutil/actions/workflows/code-quality.yml/badge.svg)
```

### 3. `publish.yml` - PyPI Publishing

**Triggers**:
- Release published (automatic)
- Manual workflow dispatch (for testing)

**Jobs**:

- **Build and Publish**:
  - Builds distribution packages
  - Validates with twine
  - Publishes to Test PyPI (manual trigger)
  - Publishes to PyPI (release trigger)

**Setup**:

1. Create PyPI API tokens:
   - Go to https://pypi.org/manage/account/token/
   - Create token with scope for this project

2. Add secrets to GitHub repository:
   - `PYPI_API_TOKEN` - For PyPI
   - `TEST_PYPI_API_TOKEN` - For Test PyPI (optional)

3. Go to: Settings → Secrets and variables → Actions → New repository secret

## Setup Instructions

### 1. Enable GitHub Actions

GitHub Actions should be enabled by default. If not:
1. Go to repository Settings
2. Click "Actions" in the left sidebar
3. Select "Allow all actions and reusable workflows"

### 2. Configure Codecov (Optional)

For coverage reporting:

1. Sign up at https://codecov.io with your GitHub account
2. Add your repository
3. Copy the upload token (usually not needed for public repos)
4. If needed, add `CODECOV_TOKEN` to repository secrets

### 3. Add Status Badges to README

Update your main `README.md`:

```markdown
# PagePool

![Tests](https://github.com/YOUR_USERNAME/pwutil/actions/workflows/test.yml/badge.svg)
![Code Quality](https://github.com/YOUR_USERNAME/pwutil/actions/workflows/code-quality.yml/badge.svg)
![Python Version](https://img.shields.io/badge/python-3.10%2B-blue)
[![codecov](https://codecov.io/gh/YOUR_USERNAME/pwutil/branch/main/graph/badge.svg)](https://codecov.io/gh/YOUR_USERNAME/pwutil)

...
```

### 4. Branch Protection (Optional but Recommended)

Set up branch protection rules:

1. Go to Settings → Branches → Add rule
2. Branch name pattern: `main` (or `master`)
3. Check:
   - ✅ Require status checks to pass before merging
   - ✅ Require branches to be up to date before merging
   - Select status checks: `Lint`, `Mock Tests`, `Demo Validation`
4. Save changes

## Workflow Details

### Test Workflow Matrix

Tests run on multiple Python versions:

| Python Version | Mock Tests | Integration Tests |
|----------------|-----------|-------------------|
| 3.10 | ✓ | ✓ |
| 3.11 | ✓ | ✓ |
| 3.12 | ✓ | ✓ |

### Caching

The workflows use GitHub Actions cache for:
- pip dependencies
- Playwright browsers (for integration tests)

This speeds up workflow runs significantly.

### Failure Handling

- **Mock tests**: Must pass (fail-fast: false to test all Python versions)
- **Integration tests**: Allowed to fail (continue-on-error: true)
- **Linting**: Informational only (continue-on-error: true)
- **Demo**: Must pass

## Local Testing

Test locally before pushing:

```bash
# Run what CI runs
python -m pytest tests/test_pagepool_simple.py -v --cov=pagepool

# Check code quality
ruff check pagepool/
black --check pagepool/ --line-length 100
mypy pagepool/ --ignore-missing-imports

# Run demo
python tests/test_pagepool_simple.py
```

## Troubleshooting

### Workflow fails on integration tests

This is expected if system dependencies are missing. Integration tests are set to `continue-on-error: true`.

Solution: Focus on mock tests, or add system dependency installation steps.

### Codecov upload fails

This is non-critical. Set `fail_ci_if_error: false` in the Codecov step (already done).

### Tests timeout

Increase timeout in workflow:

```yaml
- name: Run tests
  run: pytest tests/ -v
  timeout-minutes: 10  # Add this
```

## Monitoring

### View Workflow Runs

- Go to "Actions" tab in your GitHub repository
- Click on a workflow to see all runs
- Click on a specific run to see job details and logs

### View Coverage Reports

- Visit https://codecov.io/gh/YOUR_USERNAME/pwutil
- See coverage trends over time
- View which lines are covered/uncovered

### View Code Quality Reports

- Check "Actions" tab for latest workflow runs
- Download artifacts for detailed reports (e.g., Bandit security report)

## Best Practices

1. **Always run tests locally first** before pushing
2. **Keep workflows fast** - Mock tests should complete in < 2 minutes
3. **Don't test in production** - Use Test PyPI for testing package publishing
4. **Monitor workflow usage** - GitHub provides 2000 free minutes/month for private repos
5. **Use caching** - Reduces workflow time and GitHub Actions usage

## Updating Workflows

To modify workflows:

1. Edit `.github/workflows/*.yml` files
2. Test changes on a feature branch first
3. Verify in the Actions tab that everything works
4. Merge to main once confirmed

## Cost Considerations

For public repositories:
- ✅ **Free unlimited** workflow minutes
- ✅ Storage and bandwidth included

For private repositories:
- Free tier: 2000 minutes/month
- Each OS has different minute multipliers (Linux x1, Windows x2, macOS x10)
- Our workflows use primarily Linux, so very cost-effective

## Security

- **Never commit secrets** to the repository
- Use GitHub Secrets for API tokens
- Enable Dependabot for automated security updates
- Review Bandit security reports regularly

## See Also

- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [pytest Documentation](https://docs.pytest.org/)
- [Codecov Documentation](https://docs.codecov.com/)
