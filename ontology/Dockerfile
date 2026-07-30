# 참고용 — 지금 당장 빌드하지 않음. caren 온톨로지를 기존 moai-fuseki 공유 데이터셋에서
# 전용 인스턴스로 분리할 때 사용 (docker-compose.yml 상단 주석 참조).
#
# 사전 준비(호스트에서 1회, Docker Hub는 차단되어 있으나 mirror.kakao.com은 reachable):
#   curl -fsSL https://mirror.kakao.com/apache/jena/binaries/apache-jena-fuseki-6.1.0.tar.gz -o /tmp/fuseki.tar.gz
#   tar xzf /tmp/fuseki.tar.gz -C .   # 이 Dockerfile과 같은 디렉토리에 apache-jena-fuseki-6.1.0/ 생성
#
# base(fuseki-base:jre21)는 moai 배포 시 dnf --installroot로 로컬에서 이미 빌드해둔 이미지 —
# Docker Hub 레지스트리를 전혀 거치지 않음.
FROM fuseki-base:jre21

ARG FUSEKI_VERSION=6.1.0
COPY apache-jena-fuseki-${FUSEKI_VERSION} /opt/fuseki

ENV FUSEKI_HOME=/opt/fuseki \
    FUSEKI_BASE=/fuseki \
    JVM_ARGS=-Xmx400m

RUN mkdir -p /fuseki/databases /fuseki/logs /staging && \
    groupadd -r fuseki && useradd -r -g fuseki -d /fuseki fuseki && \
    chown -R fuseki:fuseki /opt/fuseki /fuseki && \
    chmod +x /opt/fuseki/fuseki-server

USER fuseki
WORKDIR /opt/fuseki
EXPOSE 3032
ENTRYPOINT ["/opt/fuseki/fuseki-server"]
