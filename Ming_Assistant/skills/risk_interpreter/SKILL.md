# 风险解读技能

## 输入

Risk Engine 的 `RiskResult`，包括 verdict、score、reasons、indicators、quote 和数据时间。
微观结构因子需要日内 N 分钟 K 线或逐笔 Tick 数据，标准字段为：

```text
price: 收盘/成交价格序列 P_t
volume: 成交量序列 V_t
net_flow: 净资金流序列 MF_t = 主力买入 - 主力卖出
size: 股票市值（Factor 2 截面回归）
volatility: 日内收益波动率（Factor 2 截面回归）
```

## 十个微观结构因子

计算实现位于 `app/microstructure.py`，统一入口为 `calculate_microstructure_factors(prices, volume, net_flow, residual_history=None, opening_slices=30)`。
所有实现使用 $ε = 10^{-12}$ 防止除零；返回 `None` 表示输入不足或无法识别，不能替换为 0。

### Factor 1：资金流路径震荡 × 成交量集中度

```text
VC  = Σ(V_t / ΣV_i)^2
MFO = Σ|MF_t - MF_(t-1)| / (Σ|MF_t| + ε)
F1  = VC × MFO
```

代码函数：`factor_1(volume, net_flow)`。

### Factor 2：收益偏度残差持续性

先计算 $r_t = ln(P_t/P_{t-1})$ 的偏度 `Skew_i`，再进行截面回归：

```text
Skew_i = γ0 + γ1 ln(Size_i) + γ2 Vol_i + ε_i
```

取残差的 EWMA：

```text
F2 = EWMA(ε_i, k)
```

代码函数：`factor_2_residual(skew_value, log_size, volatility, cross_section)` 计算单日残差，`factor_2(residual_history)` 计算 EWMA。没有截面样本时不得计算回归残差。

### Factor 3：资金流-收益路径震荡背离

```text
P_MF    = Σ|MF_t| / (|ΣMF_t| + ε)
P_Price = Σ|r_t| / (|P_close - P_open| / P_open + ε)
F3      = P_MF / (P_Price + ε)
```

代码函数：`factor_3(prices, net_flow)`。

### Factor 4：日内非对称波动率

```text
σ_down² = Σ I(r_t < 0) × r_t²
σ_up²   = Σ I(r_t > 0) × r_t²
F4      = (σ_down² - σ_up²) / (σ_down² + σ_up² + ε)
```

代码函数：`factor_4(prices)`。F4 越低通常表示下行半方差相对更强，但不得脱离其他指标单独判定 DANGER。

### Factor 5：资金流-成交量相关性背离

```text
ΔMF_t = MF_t - MF_(t-1)
ΔV_t  = V_t - V_(t-1)
F5    = 1 - Corr(ΔMF, ΔV)
```

代码函数：`factor_5(net_flow, volume)`。任一序列零方差时返回 `None`。

### Factor 6：三重微观结构叠加

```text
I1 = |ΣMF_t| / (Σ|MF_t| + ε)
I2 = Std(V_t) / (Mean(V_t) + ε)
I3 = 1 - |P_T - P_0| / (Σ|P_t - P_(t-1)| + ε)
F6 = Z(I1) × Z(I2) × Z(I3)
```

代码函数：`factor_6(net_flow, volume, prices, cross_section=None)`。只有传入横截面 `i1/i2/i3` 分布时才使用 Z-score；没有横截面数据时返回未标准化乘积，并必须在结果中标记为 `raw`。

### Factor 7：收益偏度-峰度交互

```text
Kurt = E[(r - mean(r))^4] / Var(r)^2 - 3
F7   = -Skew × Kurt
```

代码函数：`factor_7(prices)`。负偏度且高超额峰度会提高 F7，代表极左尾风险。

### Factor 8：成交量-收益路径震荡背离

```text
D_price = Σ|r_t|
D_vol   = ΣV_t / (max(V) × T)
F8      = D_price / (D_vol + ε)
```

代码函数：`factor_8(prices, volume)`。

### Factor 9：资金流方向强度 × 成交量波动

```text
S_flow = |ΣMF_t| / (Σ|MF_t| + ε)
V_vol  = sqrt(Σ(V_t - mean(V))² / T)
F9     = S_flow × Z(V_vol)
```

代码函数：`factor_9(net_flow, volume, volume_population=None)`。未提供横截面波动分布时不能声称结果是 Z-score。

### Factor 10：开盘冲击吸收

开盘阶段取前 `K=30` 个切片：

```text
OpenVolRatio = Σ_(t≤K)V_t / Σ_(t≤T)V_t
ImpactRatio  = Std(r_(t≤K)) / (Std(r_(t>K)) + ε)
F10          = OpenVolRatio × ImpactRatio
```

代码函数：`factor_10(prices, volume, opening_slices=30)`。不足 `K+2` 个价格切片时返回 `None`。

## 数据与判定规则

- 仅有日线 OHLCV 时，十个因子不得标记为已计算；需要明确显示 `microstructure_status=insufficient_data`。
- `net_flow`、市值或截面分布缺失时，相关因子返回 `None`，不得用价格或成交量替代。
- 因子值本身不是风险等级。`SAFE/WATCH/DANGER/ERROR` 仍由 Risk Engine 的固定阈值决定。
- 模型只能解释因子、数据时间和缺失原因，严禁改变 `score` 或 `verdict`。
- `DANGER`：明确提示暂停操作，逐条解释 `reasons`。
- `WATCH`：提示暂缓决策，说明仍需确认的信息。
- `SAFE`：只表示当前指标未触发阈值，不表示收益确定。
- `ERROR`：说明数据或计算失败，不输出方向性判断。
