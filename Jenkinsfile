// Jenkins 的建置流程。Qt 兩支檢查要用本機的 Anki.app，所以只能派給 Mac 工作機。

// 把結果寫到 GitHub 上這個 commit 旁邊（commit status）。
// token 在 Jenkins Credentials（id: github-status-token），只有這個 repo 的 Commit statuses 權限。
// 回報失敗（沒設 token、token 過期、連不上 GitHub）只印一行，不讓建置本身失敗。
def reportToGitHub(String state, String description) {
  try {
    sendStatus(state, description)
  } catch (err) {
    echo "WARNING: could not report status to GitHub: ${err}"
  }
}

def sendStatus(String state, String description) {
  withCredentials([string(credentialsId: 'github-status-token', variable: 'GH_TOKEN')]) {
    withEnv(["STATUS_STATE=${state}", "STATUS_DESC=${description}"]) {
      sh '''
        body=$(printf '{"state":"%s","context":"jenkins","description":"%s","target_url":"%s"}' \
          "$STATUS_STATE" "$STATUS_DESC" "$BUILD_URL")
        curl -fsS -o /dev/null -X POST \
          -H "Authorization: Bearer $GH_TOKEN" \
          -H "Accept: application/vnd.github+json" \
          -d "$body" \
          "https://api.github.com/repos/Kilin945/anki-white-forge/statuses/$GIT_COMMIT" \
          || echo "WARNING: could not report status to GitHub"
      '''
    }
  }
}

pipeline {
  agent { label 'mac' }

  options {
    timestamps()
    timeout(time: 20, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '30'))
  }

  stages {
    stage('Report start') {
      steps { script { reportToGitHub('pending', 'pytest + Anki Qt checks running') } }
    }
    stage('Install') {
      steps { sh 'uv sync --locked' }
    }
    stage('Unit tests') {
      steps { sh 'uv run pytest -q --junitxml=reports/pytest.xml' }
      post { always { junit 'reports/pytest.xml' } }
    }
    stage('Qt compat') {
      steps { sh 'uv run python tests/check_qt_compat.py' }
    }
    stage('Qt runtime') {
      steps { sh 'uv run python tests/check_qt_runtime.py' }
    }
  }

  post {
    success { script { reportToGitHub('success', 'pytest + Anki Qt checks passed') } }
    unsuccessful { script { reportToGitHub('failure', 'pytest or Anki Qt checks failed') } }
  }
}
