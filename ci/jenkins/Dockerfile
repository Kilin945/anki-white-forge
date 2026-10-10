FROM jenkins/jenkins:2.568.3-lts-jdk21

# 外掛在建 image 時裝好，不在網頁上手動裝；刪掉 volume 重建也是同一組外掛
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

# 設定全部來自 casc.yaml，跳過第一次開啟的安裝精靈
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"
ENV CASC_JENKINS_CONFIG=/usr/share/jenkins/ref/casc.yaml
COPY casc.yaml /usr/share/jenkins/ref/casc.yaml
