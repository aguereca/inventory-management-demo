<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else>
      <div class="card">
        <input
          type="range"
          class="budget-slider"
          v-model.number="budget"
          min="0"
          :max="sliderMax"
          step="1000"
          :aria-label="t('restocking.budget')"
        />
        <div class="stats-grid">
          <div class="stat-card info">
            <div class="stat-label">{{ t('restocking.budget') }}</div>
            <div class="stat-value">{{ formatCurrency(budget) }}</div>
          </div>
          <div class="stat-card warning">
            <div class="stat-label">{{ t('restocking.committedSpend') }}</div>
            <div class="stat-value">{{ formatCurrency(committedSpend) }}</div>
          </div>
          <div class="stat-card success">
            <div class="stat-label">{{ t('restocking.itemsSelected') }}</div>
            <div class="stat-value">{{ selected.length }}</div>
          </div>
          <div class="stat-card">
            <div class="stat-label">{{ t('restocking.remaining') }}</div>
            <div class="stat-value">{{ formatCurrency(remainingBudget) }}</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('restocking.recommendedOrders') }} ({{ selected.length }})</h3>
        </div>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>{{ t('restocking.table.sku') }}</th>
                <th>{{ t('restocking.table.itemName') }}</th>
                <th>{{ t('restocking.table.warehouse') }}</th>
                <th>{{ t('restocking.table.onHand') }}</th>
                <th>{{ t('restocking.table.reorderPoint') }}</th>
                <th>{{ t('restocking.table.qtyRecommended') }}</th>
                <th>{{ t('restocking.table.unitCost') }}</th>
                <th>{{ t('restocking.table.lineTotal') }}</th>
                <th>{{ t('restocking.table.reason') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in selected" :key="item.sku">
                <td><strong>{{ item.sku }}</strong></td>
                <td>{{ translateProductName(item.name) }}</td>
                <td>{{ translateWarehouse(item.warehouse) }}</td>
                <td>{{ item.quantity_on_hand }}</td>
                <td>{{ item.reorder_point }}</td>
                <td><strong>{{ item.qty }}</strong></td>
                <td>{{ formatCurrency(item.unit_cost) }}</td>
                <td><strong>{{ formatCurrency(item.lineTotal) }}</strong></td>
                <td>
                  <span :class="['badge', item.urgent ? 'danger' : 'warning']">
                    {{ item.urgent ? t('restocking.reasonBelowReorder') : t('restocking.reasonForecastShortfall') }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="restock-footer">
          <p v-if="confirmation">{{ confirmation }}</p>
          <button
            class="place-order-btn"
            @click="placeOrder"
            :disabled="!selected.length || submitting"
          >
            {{ submitting ? t('restocking.submitting') : t('restocking.placeOrder') }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '../api'
import { useFilters } from '../composables/useFilters'
import { useI18n } from '../composables/useI18n'
import { formatCurrency as formatCurrencyUtil } from '../utils/currency'

export default {
  name: 'Restocking',
  setup() {
    const { t, currentCurrency, translateProductName, translateWarehouse } = useI18n()

    const loading = ref(true)
    const error = ref(null)
    const submitting = ref(false)
    const confirmation = ref(null)

    const allForecasts = ref([])
    const inventoryItems = ref([])
    const budget = ref(0)
    // Only seed the slider's default (50% of max) on the very first load, so
    // reloading data after a filter change or a submitted order doesn't yank
    // the slider out from under the user.
    const budgetInitialized = ref(false)

    // Use shared filters - restocking has no time dimension and no order
    // status of its own, so only warehouse/category apply.
    const { selectedLocation, selectedCategory, getCurrentFilters } = useFilters()

    // Join forecasts to inventory and compute how much of each SKU is needed
    // to cover both the forecasted demand and the reorder point.
    const candidates = computed(() => {
      const inventoryBySku = new Map(inventoryItems.value.map(item => [item.sku, item]))
      return allForecasts.value
        .map(forecast => {
          const item = inventoryBySku.get(forecast.item_sku)
          if (!item) return null

          const qtyNeeded = Math.max(
            forecast.forecasted_demand - item.quantity_on_hand,
            item.reorder_point - item.quantity_on_hand,
            0
          )
          if (qtyNeeded === 0) return null

          return {
            sku: item.sku,
            name: item.name,
            warehouse: item.warehouse,
            quantity_on_hand: item.quantity_on_hand,
            reorder_point: item.reorder_point,
            unit_cost: item.unit_cost,
            qtyNeeded,
            fullCost: qtyNeeded * item.unit_cost,
            urgent: item.quantity_on_hand <= item.reorder_point
          }
        })
        .filter(candidate => candidate !== null)
    })

    // Urgent (at/below reorder point) items first, then by quantity needed.
    const ranked = computed(() => {
      return [...candidates.value].sort((a, b) => {
        if (a.urgent !== b.urgent) return a.urgent ? -1 : 1
        return b.qtyNeeded - a.qtyNeeded
      })
    })

    const sliderMax = computed(() => {
      const total = candidates.value.reduce((sum, c) => sum + c.fullCost, 0)
      return Math.ceil(total / 1000) * 1000
    })

    // Greedy fill: take each candidate's full recommended quantity while it
    // fits the remaining budget. The first one that doesn't fit gets a
    // partial (floor) quantity if that's at least 1 unit, then the walk
    // continues with whatever budget is left over.
    const selected = computed(() => {
      let remaining = budget.value
      const picked = []

      for (const candidate of ranked.value) {
        if (remaining <= 0) break

        if (candidate.fullCost <= remaining) {
          picked.push({ ...candidate, qty: candidate.qtyNeeded, lineTotal: candidate.fullCost })
          remaining -= candidate.fullCost
        } else {
          const partialQty = Math.floor(remaining / candidate.unit_cost)
          if (partialQty >= 1) {
            const lineTotal = partialQty * candidate.unit_cost
            picked.push({ ...candidate, qty: partialQty, lineTotal })
            remaining -= lineTotal
          }
        }
      }

      return picked
    })

    const committedSpend = computed(() => selected.value.reduce((sum, item) => sum + item.lineTotal, 0))
    const remainingBudget = computed(() => budget.value - committedSpend.value)

    // Keep the slider value valid if the candidate set shrinks (e.g. after a
    // restock order lowers demand shortfalls) and the max drops below it.
    watch(sliderMax, (newMax) => {
      if (budget.value > newMax) {
        budget.value = newMax
      }
    })

    const loadData = async () => {
      try {
        loading.value = true
        error.value = null
        const filters = getCurrentFilters()

        const [forecastData, inventoryData] = await Promise.all([
          api.getDemandForecasts(),
          api.getInventory({
            warehouse: filters.warehouse,
            category: filters.category
          })
        ])

        allForecasts.value = forecastData
        inventoryItems.value = inventoryData

        if (!budgetInitialized.value) {
          // Seed on a step=1000 boundary so the native slider thumb (which
          // snaps to steps from min) doesn't silently disagree with the
          // budget ref/stat card - e.g. 76500 would render the DOM value as
          // 77000 while the ref stayed at 76500.
          budget.value = Math.round(sliderMax.value / 2 / 1000) * 1000
          budgetInitialized.value = true
        }
      } catch (err) {
        error.value = 'Failed to load restocking data: ' + err.message
      } finally {
        loading.value = false
      }
    }

    // Watch for filter changes and reload data - warehouse/category only.
    watch([selectedLocation, selectedCategory], loadData)

    const formatCurrency = (value) => formatCurrencyUtil(value, currentCurrency.value)

    const placeOrder = async () => {
      if (!selected.value.length) return

      submitting.value = true
      error.value = null
      confirmation.value = null
      try {
        const payload = {
          items: selected.value.map(item => ({ sku: item.sku, quantity: item.qty }))
        }
        const order = await api.createRestockOrder(payload)
        confirmation.value = t('restocking.orderSubmitted', { orderNumber: order.order_number })
        await loadData()
      } catch (err) {
        const detail = err.response?.data?.detail
        error.value = detail || ('Failed to submit restock order: ' + err.message)
      } finally {
        submitting.value = false
      }
    }

    onMounted(loadData)

    return {
      t,
      loading,
      error,
      submitting,
      confirmation,
      budget,
      sliderMax,
      selected,
      committedSpend,
      remainingBudget,
      formatCurrency,
      translateProductName,
      translateWarehouse,
      placeOrder
    }
  }
}
</script>

<style scoped>
.budget-slider {
  width: 100%;
  margin-bottom: 1.25rem;
  -webkit-appearance: none;
  appearance: none;
  height: 6px;
  border-radius: 3px;
  background: #e2e8f0;
  outline: none;
}

.budget-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #3b82f6;
  cursor: pointer;
  border: 3px solid white;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
  transition: transform 0.15s ease;
}

.budget-slider::-webkit-slider-thumb:hover {
  transform: scale(1.1);
}

.budget-slider::-moz-range-thumb {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #3b82f6;
  cursor: pointer;
  border: 3px solid white;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
}

.budget-slider::-moz-range-track {
  height: 6px;
  border-radius: 3px;
  background: #e2e8f0;
}

.budget-slider:focus-visible {
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.3);
}

.restock-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin-top: 1.25rem;
  padding-top: 1rem;
  border-top: 1px solid #e2e8f0;
}

.place-order-btn {
  padding: 0.625rem 1.5rem;
  border: none;
  border-radius: 6px;
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s ease;
  background: #3b82f6;
  color: white;
  white-space: nowrap;
  margin-left: auto;
}

.place-order-btn:hover:not(:disabled) {
  background: #2563eb;
  transform: translateY(-1px);
  box-shadow: 0 2px 4px rgba(59, 130, 246, 0.3);
}

.place-order-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
