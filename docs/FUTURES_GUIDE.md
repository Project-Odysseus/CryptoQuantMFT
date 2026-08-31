# Futures Trading Guide

## Overview

**Status**: In Progress (See Issue #2)

This guide covers futures contract trading support in CryptoQuantMFT.

## Table of Contents

1. [Introduction](#introduction)
2. [Contract Specifications](#contract-specifications)
3. [Contract Sizing](#contract-sizing)
4. [Margin Requirements](#margin-requirements)
5. [Examples](#examples)
6. [Common Pitfalls](#common-pitfalls)
7. [FAQ](#faq)

---

## Introduction

### What are Futures Contracts?

**TODO**: Add introduction content

### Why Use Futures?

**TODO**: Add benefits section
- Lower margin requirements
- Leverage efficiency
- Standardized contracts
- Paper trading readiness

---

## Contract Specifications

### Contract Components

**TODO**: Document:
- Contract symbol
- Exchange
- Contract size
- Multiplier
- Tick size
- Margin requirements
- Active months

### Loading Contracts

**TODO**: Add example code for loading contracts from registry

---

## Contract Sizing

### Converting Notional to Contracts

**TODO**: Add sizing examples and walkthrough

### Rounding Behavior

**TODO**: Document rounding strategy and potential slippage

---

## Margin Requirements

### Initial Margin

**TODO**: Explain initial margin calculation

### Maintenance Margin

**TODO**: Explain maintenance margin and margin calls

### Margin Monitoring

**TODO**: Add margin tracking examples

---

## Examples

### Example 1: Basic Contract Sizing

**TODO**: Add code example

### Example 2: Portfolio with Futures

**TODO**: Add backtest example

### Example 3: Multi-Contract Portfolio

**TODO**: Add multi-contract example

---

## Common Pitfalls

**TODO**: Document:
- Rounding errors
- Margin miscalculations
- Contract expiration issues
- Leverage risks

---

## FAQ

**Q: What happens when a contract expires?**

**TODO**: Add answer

**Q: How do I handle contract rollovers?**

**TODO**: Add answer

**Q: Can I use margin/leverage?**

**TODO**: Add answer

---

## Resources

- CME Contract Specs: https://www.cmegroup.com/trading/products/
- Kraken Futures: https://support.kraken.com/hc/en-us/articles/
- Contract Registry: `config/contracts.yaml`
