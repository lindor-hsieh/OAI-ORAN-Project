/* Work-conserving integer weighted allocation for admitted new DL grants.
 * Weights are preferences, not upper bounds. Demand and slot eligibility remain
 * MAC decisions. The minimum grant is reserved before any extra RBs are shared.
 */
#ifndef NR_JOINT_PRB_H
#define NR_JOINT_PRB_H
#include <stdbool.h>
#include <math.h>

static inline bool nr_joint_prb_allocate(int count, int budget, int minimum,
                                         const double weights[], const int demand[], int grants[])
{
  if (count < 0 || budget < 0 || minimum <= 0)
    return false;
  int used = 0;
  for (int i = 0; i < count; ++i) {
    if (!isfinite(weights[i]) || weights[i] <= 0 || demand[i] < minimum)
      return false;
    grants[i] = minimum;
    used += minimum;
  }
  if (used > budget)
    return false;
  /* Each RB goes to the smallest next normalized grant. Capped/empty demand
   * leaves the competition immediately, so its unused share is redistributed.
   */
  while (used < budget) {
    int best = -1;
    double score = INFINITY;
    for (int i = 0; i < count; ++i) {
      if (grants[i] >= demand[i])
        continue;
      double next = (grants[i] + 1.0) / weights[i];
      if (next < score) {
        best = i;
        score = next;
      }
    }
    if (best < 0)
      break;
    ++grants[best];
    ++used;
  }
  return true;
}
#endif
