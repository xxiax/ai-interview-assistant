"""配置管理 REST API；秘密只写入加密存储，不通过响应返回。"""

from __future__ import annotations

from typing import Annotated, Literal

import httpx2 as httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Response, status

from . import asr, db
from .models import (
    ASRConfigData,
    ConfigResponse,
    LLMConfigData,
    LlmModelsProbeRequest,
    LlmModelsProbeResponse,
    NetworkConfigData,
    SaveConfigRequest,
    SearchConfigData,
)
from .realtime import run_db
from .security import (
    external_request_target,
    normalize_openai_base_url,
    require_auth,
    resolve_external_base_url_async,
    validate_auth_field,
    validate_external_base_url_async,
)

ConfigType = Literal["llm", "search", "asr", "network"]
router = APIRouter(
    prefix="/api/configs",
    tags=["configs"],
    dependencies=[Depends(require_auth)],
)

_DATA_MODEL_BY_TYPE = {
    "llm": LLMConfigData,
    "search": SearchConfigData,
    "asr": ASRConfigData,
    "network": NetworkConfigData,
}


def _serialized_config_data(config_type: ConfigType, req: SaveConfigRequest) -> dict:
    if config_type == "llm" and not isinstance(req.data, LLMConfigData):
        raise HTTPException(status_code=422, detail="llm 配置字段不完整或类型错误")
    if config_type == "search" and not isinstance(req.data, SearchConfigData):
        raise HTTPException(status_code=422, detail="search 配置字段不完整或类型错误")
    if config_type == "asr" and not isinstance(req.data, ASRConfigData):
        raise HTTPException(status_code=422, detail="asr 配置字段不完整或类型错误")
    if config_type == "network" and not isinstance(req.data, NetworkConfigData):
        raise HTTPException(status_code=422, detail="network 配置字段不完整或类型错误")
    data = req.data.model_dump()
    data["api_key"] = req.data.api_key.get_secret_value()
    # 新增模式不允许空密钥(编辑模式的"留空沿用"在上方 config_id 分支处理)
    if not data["api_key"]:
        raise HTTPException(status_code=422, detail=f"{config_type} 配置的 api_key 不能为空")
    return data


@router.get("/{config_type}", response_model=list[ConfigResponse])
async def get_configs(config_type: ConfigType):
    return await run_db(db.get_configs, config_type)


LLM_MODELS_MAX_ITEMS = 200
LLM_MODELS_MAX_ITEM_LENGTH = 200


@router.post("/llm/models", response_model=LlmModelsProbeResponse)
async def probe_llm_models(req: LlmModelsProbeRequest):
    """代理第三方 {base_url}/models，客户端不必直接持有 Key。

    api_key 为空时复用当前激活 LLM 配置中已保存的 Key（用户在设置页
    "获取模型"按钮的常见场景：先保存配置再拉列表）。
    """
    try:
        endpoint = await resolve_external_base_url_async(
            normalize_openai_base_url(req.base_url)
        )
        base_url = endpoint.original_url
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    api_key = req.api_key.strip()
    auth_field = req.auth_field
    if not api_key:
        config = await run_db(db.get_active_config, "llm")
        configured_base_url = (
            normalize_openai_base_url(
                str(config["data"].get("base_url", ""))
            )
            if config
            else ""
        )
        if config and configured_base_url == base_url:
            api_key = str(config["data"].get("api_key", "")).strip()
            auth_field = validate_auth_field(
                str(config["data"].get("auth_field", "Authorization"))
            )
        elif config:
            raise HTTPException(
                status_code=400,
                detail="目标 Base URL 已改变，请显式填写该服务的 API Key",
            )
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="缺少 API Key：请填写或先保存激活一个 LLM 配置",
        )

    try:
        client_kwargs = asr.http_client_kwargs(httpx.Timeout(15.0, connect=10.0))
        request_base_url, request_extensions = external_request_target(
            endpoint, client_kwargs
        )
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(
                f"{request_base_url}/models",
                headers={
                    auth_field: (
                        f"Bearer {api_key}"
                        if auth_field.lower() == "authorization"
                        else api_key
                    ),
                    "Host": endpoint.host_header,
                },
                extensions=request_extensions,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="模型列表获取失败") from exc

    if resp.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="API Key 无效或无权限")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="模型列表获取失败")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="模型列表获取失败") from exc
    data_items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data_items, list):
        data_items = []
    models = sorted(
        {
            str(item.get("id", "")).strip()[:LLM_MODELS_MAX_ITEM_LENGTH]
            for item in data_items
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }
    )
    return LlmModelsProbeResponse(models=models[:LLM_MODELS_MAX_ITEMS])


@router.post(
    "/{config_type}", response_model=ConfigResponse, status_code=status.HTTP_201_CREATED
)
async def save_config(config_type: ConfigType, req: SaveConfigRequest):
    if config_type == "llm" and isinstance(req.data, LLMConfigData):
        try:
            await validate_external_base_url_async(req.data.base_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if req.config_id is not None:
        # 编辑模式:更新已有配置;api_key 允许留空沿用旧密钥。
        if not isinstance(req.data, _DATA_MODEL_BY_TYPE[config_type]):
            raise HTTPException(
                status_code=422, detail=f"{config_type} 配置字段不完整或类型错误"
            )
        data = req.data.model_dump()
        secret = req.data.api_key.get_secret_value()
        if secret:
            data["api_key"] = secret
        try:
            return await run_db(
                db.update_config,
                config_type,
                req.config_id,
                req.name,
                data,
                req.is_active,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="配置不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    data = _serialized_config_data(config_type, req)
    try:
        return await run_db(db.save_config, config_type, req.name, data, req.is_active)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{config_type}/activate/{config_id}", response_model=ConfigResponse)
async def activate_config(
    config_type: ConfigType,
    config_id: Annotated[int, Path(ge=1, le=db.SQLITE_INT_MAX)],
):
    try:
        return await run_db(db.activate_config, config_type, config_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="配置不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete(
    "/{config_type}/{config_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_config(
    config_type: ConfigType,
    config_id: Annotated[int, Path(ge=1, le=db.SQLITE_INT_MAX)],
):
    try:
        await run_db(db.delete_config, config_type, config_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="配置不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
