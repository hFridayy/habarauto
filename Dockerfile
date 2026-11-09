FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY habar ./habar
RUN pip install --no-cache-dir .
ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
USER nobody
CMD ["habar"]
