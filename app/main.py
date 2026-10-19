                },
            )
            chain_response.raise_for_status()
            chain_payload = chain_response.json()

        chain = chain_payload.get("data", [])

        if not isinstance(chain, list) or not chain:
            return {
                "ok": False,
                "symbol": symbol,
                "expiry": selected_expiry,
                "available_expiries": expiries,
                "message": "Option chain is empty.",
            }

        valid_rows = []

        for row in chain:
            if not isinstance(row, dict):
                continue

            try:
                strike = float(row["strike_price"])
                spot = float(row["underlying_spot_price"])
            except (KeyError, TypeError, ValueError):
                continue

            if strike > 0 and spot > 0:
                valid_rows.append((strike, spot, row))

        if not valid_rows:
            return {
                "ok": False,
                "symbol": symbol,
                "message": "No valid strike or spot price found.",
            }

        # Use the most frequent underlying spot if the API has repeated rows.
        spot = valid_rows[0][1]
        strikes = sorted({item[0] for item in valid_rows})
        atm_strike = min(strikes, key=lambda strike: abs(strike - spot))

        ce_candidates = []
        pe_candidates = []

        for strike, _, row in valid_rows:
            if moneyness == "ATM":
                if strike == atm_strike:
                    ce_candidates.append((0, strike, row))
                    pe_candidates.append((0, strike, row))

            elif moneyness == "ITM":
                # Call ITM: strike below or equal to spot.
                if strike <= spot:
                    ce_candidates.append((spot - strike, strike, row))

                # Put ITM: strike above or equal to spot.
                if strike >= spot:
                    pe_candidates.append((strike - spot, strike, row))

            else:  # OTM
                # Call OTM: strike above or equal to spot.
                if strike >= spot:
                    ce_candidates.append((strike - spot, strike, row))

                # Put OTM: strike below or equal to spot.
                if strike <= spot:
                    pe_candidates.append((spot - strike, strike, row))

        def choose(candidates):
            if not candidates:
                return None
            return min(candidates, key=lambda item: item[0])[2]

        ce = option_details(choose(ce_candidates), "CE")
        pe = option_details(choose(pe_candidates), "PE")

        return {
            "ok": bool(ce or pe),
            "symbol": symbol,
            "expiry": selected_expiry,
            "available_expiries": expiries,
            "moneyness": moneyness,
            "spot": spot,
            "atm_strike": atm_strike,
            "ce": ce,
            "pe": pe,
            "fetched_at": utc_now().isoformat(),
            "quote_timestamp_available": False,
            "quote_freshness_verified": False,
            "source": "Upstox Option Chain API",
            "message": (
                "Option-chain response received. fetched_at is server "
                "fetch time, not an exchange quote timestamp. Quote "
                "freshness is not verified; do not treat this as a trade signal."
            ),
        }

    except httpx.HTTPStatusError as exc:
        return {
            "ok": False,
            "symbol": symbol,
            "message": (
                f"Upstox returned HTTP {exc.response.status_code}. "
                "Check API access, token validity, permissions, and Render logs."
            ),
        }

    except httpx.HTTPError:
        return {
            "ok": False,
            "symbol": symbol,
            "message": "Could not connect to the Upstox option API.",
        }

    except (ValueError, TypeError, KeyError):
        return {
            "ok": False,
            "symbol": symbol,
            "message": "Unexpected or invalid option-chain response.",
        }
