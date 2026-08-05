# A2A HITL UI Extension v1

(원본: github.com/genonai/GenOS docs/a2a/EXTENSION_HITL_UI_v1.md — 2026-08-05 수신)

A2A 에이전트가 작업 도중 사용자 입력을 요청하는 규약이다.

| 항목 | 값 |
| --- | --- |
| Extension URI | `https://genos.genon.ai/a2a/extensions/hitl-ui/v1` |
| A2A 상태 | `input-required` |
| 지원 컴포넌트 | `confirm`, `single-select`, `multi-select` |

`text-input`은 이전 요청과의 호환을 위해 스키마에만 남겨 둔다. 자유 입력은 선택형 컴포넌트의 마지막 `직접 입력` 항목으로 받는다.

## 공통

### 활성화

Agent Card의 `capabilities.extensions`에 Extension을 선언한다.

```json
{ "uri": "https://genos.genon.ai/a2a/extensions/hitl-ui/v1", "required": false }
```

마스터 Agent에서 `사용자 확인 요청 전달(HITL)`을 켜면 Extension URI가 A2A 요청에 포함된다. 서브에이전트에는 `request_user_input` Tool이 자동으로 제공되므로 프롬프트에 JSON 형식을 지정할 필요가 없다.

### Tool 요청

Agent는 사용자 입력 없이는 작업을 계속할 수 없을 때 `request_user_input`을 호출한다.

```json
{
  "question": "조회할 항목을 선택해 주세요.",
  "type": "single-select",
  "options": [
    { "value": "balance", "label": "잔액" },
    { "value": "transactions", "label": "거래 내역", "desc": "최근 입출금과 카드 승인 내역을 조회합니다." }
  ]
}
```

| 필드 | 설명 |
| --- | --- |
| `question` | 사용자에게 표시할 질문 |
| `type` | 표시할 컴포넌트 |
| `options` | 선택형 컴포넌트의 선택지 |
| `options[].value` | Agent에 돌려줄 값 |
| `options[].label` | 화면에 표시할 이름 |
| `options[].desc` | 라벨 아래에 표시할 설명. 생략 가능 |

Tool 결과는 런타임에서 다음 형태의 `input-required` Task로 변환된다.

```json
{
  "a2a_status": "input-required",
  "text": "조회할 항목을 선택해 주세요.",
  "component": {
    "type": "single-select",
    "options": [
      { "value": "balance", "label": "잔액" },
      { "value": "transactions", "label": "거래 내역" }
    ]
  }
}
```

질문은 `Part.text`, 컴포넌트와 런타임이 발급한 `interactionId`는 `Part.data`에 저장된다. 기존 JSON 응답 파싱은 호환용 fallback으로 유지한다.

### 사용자 응답

선택 또는 직접 입력은 `values`에 넣는다.

```json
{
  "interactionId": "hitl-123",
  "action": "submit",
  "values": { "selected": ["balance"], "customInput": "해외 결제만 표시" }
}
```

취소는 선택값 없이 보낸다.

```json
{ "interactionId": "hitl-123", "action": "cancel" }
```

`submit`은 Flowise의 `proceed`, `cancel`은 `reject`로 전달된다. Workflow와 Flowise는 `component`, `interactionId`, `values`를 변경하지 않는다.

재개 메시지에는 원 질문과 컴포넌트, 선택한 값과 라벨을 함께 넣는다. 서브에이전트는 이를 새 요청이 아닌 기존 `interactionId`의 응답으로 처리한다. Extension이 활성화된 A2A 인터럽트에 컴포넌트가 없으면 레거시 UI 대신 `confirm`으로 표시한다.

### 화면 동작

- HITL UI는 에이전트 메시지 위치에 표시한다.
- 선택형 컴포넌트에는 마지막 항목으로 `직접 입력`을 자동 추가한다.
- 직접 입력란은 한 줄에서 시작해 최대 다섯 줄까지 늘어난다.
- 제출 후와 히스토리에서는 선택 결과를 disabled 상태로 표시한다. 직접 입력값은 입력란 없이 텍스트로 표시한다.
- X 버튼은 확인창 없이 요청을 취소한다. 처리 중에는 같은 위치에 spinner를 표시한다.
- 취소 후에는 채팅을 계속할 수 있다. 다시 HITL이 필요하면 새 `interactionId`로 요청한다.

## 컴포넌트

### `confirm`
작업 진행 여부를 묻는다. `options`는 사용하지 않는다. 진행은 `submit`, 거절은 `cancel`.

### `single-select`
radio button. 일반 선택 `{ "selected": ["balance"] }`, 직접 입력 `{ "selected": [], "customInput": "..." }` — 동시 사용 불가.

### `multi-select`
checkbox. 일반 선택과 직접 입력 동시 제출 가능:
`{ "selected": ["balance", "transactions"], "customInput": "해외 결제만 표시" }`

## 검증 규칙

- 선택형 컴포넌트에는 중복되지 않는 선택지가 한 개 이상 있어야 한다.
- `value`, `label`, `desc`는 값이 있을 때 빈 문자열일 수 없다.
- `customInput`은 공백만 입력한 상태로 제출할 수 없다.
- 알 수 없는 컴포넌트는 오류로 처리한다.
