import matplotlib.pyplot as plt
import numpy as np

force = np.loadtxt("force.txt")
d = np.loadtxt("d.txt")
ddde = np.loadtxt("de.txt")
fi = np.loadtxt("fi.txt")
force = np.linalg.norm(force, axis=1)
fi = fi[1::2]
ddde = ddde[1::2]
d = d[1::2]
# result = np.vstack((ddde))
# result = result[:, 1500:-1]
# plt.plot(result[1], ".:", label='x')
# plt.plot(result[0], ":", label='f')
# ddde = -np.sum(ddde, 1)
result = np.vstack((ddde, d, fi))
plt.plot(result[0], label='de')
# plt.plot(result[1], ".:", label='x')
plt.plot(result[2], ":", label='f')
plt.legend(fontsize=15)
plt.show()
