#include <assert.h>
#include <stdio.h>
#include "openair2/LAYER2/NR_MAC_gNB/nr_joint_prb.h"

int main(void)
{
  double w[] = {20, 80};
  int d[] = {106, 106}, g[2];
  assert(nr_joint_prb_allocate(2, 106, 5, w, d, g));
  assert(g[0] == 21 && g[1] == 85);
  d[0] = 8;
  assert(nr_joint_prb_allocate(2, 106, 5, w, d, g));
  assert(g[0] == 8 && g[1] == 98);
  assert(nr_joint_prb_allocate(1, 106, 5, w + 1, d + 1, g));
  assert(g[0] == 106); /* The other UE failed ACK admission: no reserved share. */
  assert(!nr_joint_prb_allocate(2, 9, 5, w, d, g));
  d[0] = 8; d[1] = 10;
  assert(nr_joint_prb_allocate(2, 106, 5, w, d, g));
  assert(g[0] + g[1] == 18);
  /* Exhaustive small budgets: conservation, demand bounds and no avoidable idle RBs. */
  for (int budget = 10; budget <= 106; ++budget)
    for (int a = 5; a <= 110; ++a)
      for (int b = 5; b <= 110; ++b) {
        d[0] = a; d[1] = b;
        assert(nr_joint_prb_allocate(2, budget, 5, w, d, g));
        assert(g[0] >= 5 && g[1] >= 5 && g[0] <= a && g[1] <= b);
        assert(g[0] + g[1] == (a + b < budget ? a + b : budget));
      }
  puts("PASS: weighted share, ACK-excluded UE, demand redistribution and exhaustive conservation");
}
