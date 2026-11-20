#!/bin/sh
# 容器健康检查：端口从挂载的 config.json 的 listen 字段推导，避免 EXPOSE/健康检查
# 与实际监听端口不一致（本镜像支持任意端口部署，如 :7863 与 :1114 并行）。
# listen 支持 ":1114"、"0.0.0.0:1114" 两种形态，取最后一个冒号后的段。
set -e
PORT=$(python3 -c 'import json,sys; l=json.load(open("/app/config.json")).get("listen",":7863"); print(l.rsplit(":",1)[-1])')
exec wget -qO- "http://127.0.0.1:${PORT}/healthz"