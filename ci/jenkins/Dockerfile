FROM jenkins/jenkins:2.568.3-lts-jdk21

# 外掛在建 image 時裝好，不在網頁上手動裝；刪掉 volume 重建也是同一組外掛
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

# 設定全部來自 casc.yaml，跳過第一次開啟的安裝精靈
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"
# casc.yaml 由 docker-compose 掛進來，改設定只要重啟容器、不用重建 image
ENV CASC_JENKINS_CONFIG=/var/jenkins_casc/casc.yaml
