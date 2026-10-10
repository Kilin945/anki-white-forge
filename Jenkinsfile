// Jenkins 的建置流程。Qt 兩支檢查要用本機的 Anki.app，所以只能派給 Mac 工作機。
pipeline {
  agent { label 'mac' }

  options {
    timestamps()
    timeout(time: 20, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '30'))
  }

  stages {
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
}
