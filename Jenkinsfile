// Jenkins 的建置流程。Qt 兩支檢查要用本機的 Anki.app，所以只能派給 Mac 工作機。

// 把結果寫到 GitHub 上這個 commit 旁邊（commit status）。
// token 在 Jenkins Credentials（id: github-status-token），只有這個 repo 的 Commit statuses 讀寫和 Contents 唯讀權限。
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

// 寄信給 NOTIFY_EMAIL（casc.yaml 的全域變數，信箱不寫在這裡）。
// 沒設收件人就不寄；寄信失敗只印一行，不讓建置本身失敗。
def notifyByEmail(String headline) {
  if (!env.NOTIFY_EMAIL?.trim()) {
    echo 'NOTIFY_EMAIL is not set, skip email'
    return
  }
  def commit = env.GIT_COMMIT ?: 'unknown'
  try {
    mail(
      to: env.NOTIFY_EMAIL,
      subject: "[anki-white-forge] Build #${env.BUILD_NUMBER} ${headline}",
      body: """Build #${env.BUILD_NUMBER} ${headline}.

Commit: https://github.com/Kilin945/anki-white-forge/commit/${commit}
Build log: ${env.BUILD_URL}console
Test report: ${env.BUILD_URL}testReport/
"""
    )
  } catch (err) {
    echo "WARNING: could not send email: ${err}"
  }
}

// 回報到 GitHub 的說明文字；量到覆蓋率時附在後面
def withCoverage(String text) {
  return env.COVERAGE_PCT ? "${text} · coverage ${env.COVERAGE_PCT}%" : text
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
    // 三組測試的覆蓋率記在同一份 .coverage：pytest 開頭會清掉上次的紀錄，兩支 Qt 檢查用 --append 累加
    stage('Unit tests') {
      steps { sh 'uv run pytest -q --junitxml=reports/pytest.xml --cov=addon --cov=core --cov-report=' }
      post { always { junit 'reports/pytest.xml' } }
    }
    stage('Qt compat') {
      steps { sh 'uv run coverage run --append --source=addon,core tests/check_qt_compat.py' }
    }
    stage('Qt runtime') {
      steps { sh 'uv run coverage run --append --source=addon,core tests/check_qt_runtime.py' }
    }
    stage('Coverage') {
      steps {
        sh 'uv run coverage xml -o reports/coverage.xml'
        script {
          env.COVERAGE_PCT = sh(script: 'uv run coverage report --format=total', returnStdout: true).trim()
        }
        // 低於 85% 就把建置標成失敗（會回報 ✗ 並寄信）
        recordCoverage(
          tools: [[parser: 'COBERTURA', pattern: 'reports/coverage.xml']],
          sourceCodeRetention: 'EVERY_BUILD',
          qualityGates: [[threshold: 85.0, metric: 'LINE', baseline: 'PROJECT', criticality: 'FAILURE']]
        )
      }
    }
  }

  post {
    success { script { reportToGitHub('success', withCoverage('pytest + Anki Qt checks passed')) } }
    unsuccessful { script { reportToGitHub('failure', withCoverage('build failed')) } }
    // 只在失敗、以及從失敗恢復時寄信；平常的成功不寄
    failure { script { notifyByEmail('failed') } }
    fixed { script { notifyByEmail('is back to normal') } }
  }
}
