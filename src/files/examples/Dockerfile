FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
COPY generate.py /generate
RUN chmod 755 /generate
USER 65534:65534
ENTRYPOINT ["/generate"]
